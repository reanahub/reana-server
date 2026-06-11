# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Stateless JWT access-token validation against the trusted issuer."""

import logging
import threading
import time

import requests
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError

from reana_server.config import REANA_AUTH
from reana_server.auth.discovery import get_endpoint
from reana_server.auth.errors import InvalidTokenError, MissingRoleError

ALLOWED_ALGORITHMS = ["RS256", "ES256"]
"""Accepted JWT signing algorithms (``none`` and HMAC are never accepted)."""

_jwt = JsonWebToken(ALLOWED_ALGORITHMS)


class JWKSCache:
    """In-process TTL cache of the issuer's JSON Web Key Set.

    A key rotation at the issuer is handled by the caller retrying once
    with ``force=True`` when the token's ``kid`` is unknown.
    """

    def __init__(self, ttl):
        """Initialize the cache with a time-to-live in seconds."""
        self.ttl = ttl
        self._lock = threading.Lock()
        self._key_set = None
        self._fetched_at = 0.0

    def get_key_set(self, force=False):
        """Return the issuer's key set, refetching when stale or forced."""
        with self._lock:
            fresh = time.monotonic() - self._fetched_at < self.ttl
            if self._key_set is not None and fresh and not force:
                return self._key_set
            jwks_url = get_endpoint("jwks_url")
            try:
                response = requests.get(
                    jwks_url, timeout=REANA_AUTH["http_timeout"]
                )
                response.raise_for_status()
                jwks = response.json()
            except (requests.RequestException, ValueError) as error:
                if self._key_set is not None:
                    # Serve the stale key set rather than rejecting all
                    # requests during a transient issuer outage (signature
                    # verification stays offline, see AUTH_ARCHITECTURE §16.9).
                    logging.warning(
                        "Could not refresh JWKS from %s, serving cached key "
                        "set: %s",
                        jwks_url,
                        error,
                    )
                    return self._key_set
                raise InvalidTokenError(
                    f"Could not fetch JWKS from {jwks_url}: {error}"
                )
            if not jwks.get("keys"):
                raise InvalidTokenError("Issuer's JWKS contains no keys.")
            self._key_set = JsonWebKey.import_key_set(jwks)
            self._fetched_at = time.monotonic()
            return self._key_set


_jwks_cache = None


def _get_jwks_cache():
    global _jwks_cache
    if _jwks_cache is None:
        _jwks_cache = JWKSCache(ttl=REANA_AUTH["jwks_ttl"])
    return _jwks_cache


def _claims_options():
    options = {
        "iss": {"essential": True, "value": REANA_AUTH["issuer"]},
        "exp": {"essential": True},
        "sub": {"essential": True},
    }
    if REANA_AUTH["audience"]:
        options["aud"] = {"essential": True, "value": REANA_AUTH["audience"]}
    return options


def validate_access_token(token):
    """Validate a JWT access token and return its claims.

    Enforces: signature against the issuer's JWKS (cached, with one forced
    refetch on unknown ``kid`` to cover key rotation), algorithm allowlist,
    ``iss`` pinned to the configured issuer, ``aud`` containing the
    configured audience (unless audience checking is disabled), ``exp``/
    ``nbf`` with the configured leeway, and presence of ``sub``.

    :raises InvalidTokenError: when the token fails any of the above.
    """
    if not REANA_AUTH["issuer"]:
        raise InvalidTokenError(
            "JWT authentication is not configured (REANA_AUTH_ISSUER unset)."
        )
    cache = _get_jwks_cache()
    claims_options = _claims_options()
    try:
        try:
            claims = _jwt.decode(
                token, cache.get_key_set(), claims_options=claims_options
            )
        except (JoseError, ValueError):
            # Unknown ``kid`` (key rotation) surfaces as ValueError from the
            # key-set lookup; bad signatures as JoseError. One forced JWKS
            # refresh covers rotation; genuine bad tokens fail again below.
            claims = _jwt.decode(
                token,
                cache.get_key_set(force=True),
                claims_options=claims_options,
            )
        claims.validate(leeway=REANA_AUTH["leeway"])
    except JoseError as error:
        raise InvalidTokenError(f"Invalid access token: {error}")
    except ValueError as error:
        raise InvalidTokenError(f"Invalid access token: {error}")
    return claims


def extract_roles(claims, userinfo=None):
    """Return the REANA roles found in token claims or userinfo."""
    roles_claim = REANA_AUTH["roles_claim"]
    roles = claims.get(roles_claim)
    if roles is None and userinfo is not None:
        roles = userinfo.get(roles_claim)
    if isinstance(roles, str):
        roles = [roles]
    return list(roles or [])


def require_role(claims, userinfo=None):
    """Enforce the configured required role (default ``reana:user``).

    The role replaces the legacy "user has an active token" gate. Roles are
    read from the configured roles claim (``reana_roles``) in the access
    token, falling back to the userinfo response. An empty
    ``REANA_AUTH_REQUIRED_ROLE`` disables the gate (every authenticated
    user of the issuer may use REANA).

    :raises MissingRoleError: when the required role is absent.
    """
    required = REANA_AUTH["required_role"]
    if not required:
        return
    roles = extract_roles(claims, userinfo)
    if required not in roles:
        raise MissingRoleError(
            f"User does not have the required '{required}' role."
        )
