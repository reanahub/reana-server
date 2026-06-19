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


def _validate_access_token_jwt(token):
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


def _validate_access_token_introspection(token):
    """Validate an access token via the issuer's introspection endpoint."""
    try:
        response = requests.post(
            get_endpoint("introspection_url"),
            data={"token": token},
            auth=(
                REANA_AUTH["introspection_client_id"],
                REANA_AUTH["introspection_client_secret"],
            ),
            timeout=REANA_AUTH["http_timeout"],
        )
        response.raise_for_status()
        claims = response.json()
    except (requests.RequestException, ValueError) as error:
        raise InvalidTokenError(f"Could not introspect access token: {error}")
    if claims.get("active") is not True:
        raise InvalidTokenError("Access token is not active.")
    if claims.get("iss") and claims["iss"] != REANA_AUTH["issuer"]:
        raise InvalidTokenError("Invalid access token issuer.")
    if not claims.get("sub"):
        raise InvalidTokenError(
            "Access token introspection response is missing 'sub'."
        )
    audience = REANA_AUTH["audience"]
    if audience:
        aud = claims.get("aud")
        aud = aud if isinstance(aud, list) else [aud]
        if audience not in aud:
            raise InvalidTokenError("Invalid access token audience.")
    exp = claims.get("exp")
    if exp is not None and int(exp) + REANA_AUTH["leeway"] < int(time.time()):
        raise InvalidTokenError("Access token is expired.")
    return claims


def validate_access_token(token):
    """Validate an access token and return its claims.

    JWT/JWKS validation is the default. Operators can opt into issuer-side
    introspection for opaque EOSC access tokens via
    ``REANA_AUTH_TOKEN_VALIDATION=introspection`` or use ``auto`` to try JWT
    validation first and fall back to introspection.

    :raises InvalidTokenError: when the token fails validation.
    """
    mode = REANA_AUTH.get("token_validation", "jwt")
    if mode == "jwt":
        return _validate_access_token_jwt(token)
    if mode == "introspection":
        return _validate_access_token_introspection(token)
    if mode == "auto":
        try:
            return _validate_access_token_jwt(token)
        except InvalidTokenError as jwt_error:
            logging.debug(
                "JWT validation failed, trying introspection: %s", jwt_error
            )
            return _validate_access_token_introspection(token)
    raise InvalidTokenError(f"Unsupported token validation mode: {mode!r}.")


def validate_id_token(id_token, nonce=None):
    """Validate an OIDC ID token returned by the BFF code flow.

    The access token remains the authorization credential; the ID token is
    validated to bind the browser authorization response to the login request
    via ``nonce`` and to catch issuer/client mix-ups early.

    :raises InvalidTokenError: when the token is absent or invalid.
    """
    if not id_token:
        raise InvalidTokenError("Issuer did not return an ID token.")
    if not REANA_AUTH["issuer"]:
        raise InvalidTokenError(
            "OIDC authentication is not configured (REANA_AUTH_ISSUER unset)."
        )
    claims_options = {
        "iss": {"essential": True, "value": REANA_AUTH["issuer"]},
        "exp": {"essential": True},
        "sub": {"essential": True},
    }
    if REANA_AUTH["web_client_id"]:
        claims_options["aud"] = {
            "essential": True,
            "value": REANA_AUTH["web_client_id"],
        }
    try:
        try:
            claims = _jwt.decode(
                id_token,
                _get_jwks_cache().get_key_set(),
                claims_options=claims_options,
            )
        except (JoseError, ValueError):
            claims = _jwt.decode(
                id_token,
                _get_jwks_cache().get_key_set(force=True),
                claims_options=claims_options,
            )
        claims.validate(leeway=REANA_AUTH["leeway"])
    except JoseError as error:
        raise InvalidTokenError(f"Invalid ID token: {error}")
    except ValueError as error:
        raise InvalidTokenError(f"Invalid ID token: {error}")
    if nonce is not None and claims.get("nonce") != nonce:
        raise InvalidTokenError("Invalid ID token nonce.")
    return claims


def _get_claim_path(document, path):
    """Return a nested claim value using dot-separated path syntax."""
    value = document or {}
    for part in (path or "").split("."):
        if not part:
            return None
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _as_role_list(value):
    """Normalize an issuer role claim value to a list of strings."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [role for role in value if isinstance(role, str)]
    return []


def _map_roles(roles, mapping, match="exact"):
    """Map provider roles to REANA roles according to one role source.

    ``match`` controls how claim values are matched against ``mapping`` keys:
    - ``"exact"`` (default): ``mapping[role]``
    - ``"startswith"``: first mapping key that is a prefix of the role value
      (useful for EOSC entitlement URNs where the ``#<authority>`` suffix
      varies across EOSC AAI proxy instances)
    """
    if not isinstance(mapping, dict):
        return roles
    mapped_roles = []
    for role in roles:
        if match == "startswith":
            mapped = next(
                (v for k, v in mapping.items() if role.startswith(k)), None
            )
        else:
            mapped = mapping.get(role)
        if mapped is None:
            continue
        mapped_roles.extend(_as_role_list(mapped))
    return mapped_roles


def extract_roles(claims, userinfo=None):
    """Return REANA roles found in configured token/userinfo sources."""
    role_sources = REANA_AUTH.get("role_sources") or [
        {"path": REANA_AUTH["roles_claim"]}
    ]
    roles = []
    for source in role_sources:
        if not isinstance(source, dict):
            continue
        path = source.get("path") or source.get("claim")
        raw_roles = _get_claim_path(claims, path)
        if raw_roles is None and userinfo is not None:
            raw_roles = _get_claim_path(userinfo, path)
        source_roles = _as_role_list(raw_roles)
        roles.extend(
            _map_roles(source_roles, source.get("map"), source.get("match", "exact"))
        )

    # Preserve order for predictable logs/tests while removing duplicates.
    unique_roles = []
    seen = set()
    for role in roles:
        if role not in seen:
            unique_roles.append(role)
            seen.add(role)
    return unique_roles


def role_sources_need_userinfo(claims):
    """Return whether configured role sources may require UserInfo fallback."""
    required = REANA_AUTH["required_role"]
    if not required or required in extract_roles(claims):
        return False
    role_sources = REANA_AUTH.get("role_sources") or [
        {"path": REANA_AUTH["roles_claim"]}
    ]
    for source in role_sources:
        if not isinstance(source, dict):
            continue
        path = source.get("path") or source.get("claim")
        if _get_claim_path(claims, path) is None:
            return True
    return False


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
