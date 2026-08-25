# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Stateless JWT access-token validation against the trusted issuer."""

import base64
import json
import logging
import threading
import time

import requests
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from flask import current_app

from reana_server.auth.config import (
    get_auth_config,
    get_issuer_request_kwargs,
    raise_for_issuer_status,
)
from reana_server.auth.discovery import get_endpoint
from reana_server.auth.errors import (
    AuthError,
    InvalidTokenError,
    IssuerKeyUnavailableError,
    IssuerMisconfiguredError,
    IssuerUnavailableError,
    MissingRoleError,
    UnknownKeyHealthyBackoffError,
)

ALLOWED_ALGORITHMS = ["RS256", "ES256"]
"""Accepted JWT signing algorithms (``none`` and HMAC are never accepted)."""

_jwt = JsonWebToken(ALLOWED_ALGORITHMS)
_JWKS_EXTENSION = "reana_auth_jwks"
_MIN_UNKNOWN_KID_REFRESH_INTERVAL = 5
_UNKNOWN_KID_TTL = 60
_MAX_UNKNOWN_KIDS = 256
"""Hard cap on negative-cached key ids, so a flood of attacker-chosen ``kid``
headers cannot grow a worker's memory without bound."""


class JWKSCache:
    """In-process TTL cache of the issuer's JSON Web Key Set.

    A key rotation at the issuer is handled by refreshing only when a token
    carries a non-empty ``kid`` absent from the cached key set.
    """

    def __init__(self, ttl, refresh_wait_timeout=30, stale_grace=3600):
        """Initialize the cache with a time-to-live in seconds."""
        self.ttl = ttl
        self.stale_grace = stale_grace
        self.refresh_wait_timeout = refresh_wait_timeout
        self._lock = threading.Lock()
        self._refresh_condition = threading.Condition(self._lock)
        self._refresh_in_progress = False
        self._refresh_generation = 0
        self._refresh_failed_at = 0.0
        self._key_set = None
        self._known_kids = set()
        self._unknown_kids = {}
        self._fetched_at = 0.0
        self._last_unknown_kid_refresh = 0.0

    def _cached_keys_are_usable(self, now):
        """Return whether cached keys are inside their bounded stale window.

        Callers hold ``self._lock``. Fresh keys are necessarily usable; after
        the normal TTL, a transient issuer failure may reuse them only for the
        configured additional grace period. At the exact boundary validation
        fails closed.
        """
        return self._key_set is not None and (
            now - self._fetched_at < self.ttl + self.stale_grace
        )

    def _serve_stale_or_raise(self, now, refresh_error=None):
        """Return cached keys inside the grace period or fail closed."""
        if self._cached_keys_are_usable(now):
            if refresh_error is not None:
                logging.warning(
                    "Could not refresh JWKS; serving cached key set: %s",
                    refresh_error,
                )
            return self._key_set
        error = IssuerKeyUnavailableError(
            "Cached issuer keys have exceeded their stale grace period."
        )
        if refresh_error is not None:
            raise error from refresh_error
        raise error

    def _fetch(self):
        """Fetch and validate a JWKS without holding the cache mutex.

        ``IssuerMisconfiguredError`` (e.g. from ``get_endpoint`` resolving
        ``jwks_url``, or from discovery underneath it) is re-raised as-is
        ahead of the broader transport/decoding catch below, even though it
        is-an ``AuthError`` and would otherwise match that tuple too: it is a
        permanent configuration defect, not a transient issuer outage, and
        must not be treated as one -- see ``_refresh`` for why serving the
        stale cached key set for this error family would silently mask it
        forever once the cache is warm.
        """
        jwks_url = None
        try:
            jwks_url = get_endpoint("jwks_url")
            response = requests.get(jwks_url, **get_issuer_request_kwargs())
            raise_for_issuer_status(response)
            jwks = response.json()
        except IssuerMisconfiguredError:
            raise
        except (requests.RequestException, ValueError, AuthError) as error:
            issuer_location = jwks_url or "the configured issuer"
            if self._key_set is not None:
                # Serve the stale key set rather than rejecting all requests
                # during a transient issuer outage. Signature verification
                # still happens locally with the cached keys.
                logging.warning(
                    "Could not refresh JWKS from %s, serving cached key set: %s",
                    issuer_location,
                    error,
                )
                raise
            raise IssuerUnavailableError(
                f"Could not fetch JWKS from {issuer_location}: {error}"
            ) from error
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise IssuerKeyUnavailableError("Issuer's JWKS is malformed.")
        if not jwks["keys"]:
            raise IssuerKeyUnavailableError("Issuer's JWKS contains no keys.")
        try:
            key_set = JsonWebKey.import_key_set(jwks)
        except (TypeError, ValueError) as error:
            raise IssuerKeyUnavailableError(
                f"Issuer's JWKS is malformed: {error}"
            ) from error
        known_kids = {
            key["kid"]
            for key in jwks["keys"]
            if isinstance(key, dict) and isinstance(key.get("kid"), str) and key["kid"]
        }
        return key_set, known_kids

    def _refresh(self, wait_for_initial=False, require_fresh=False):
        """Refresh once, with network I/O outside the state mutex."""
        with self._refresh_condition:
            now = time.monotonic()
            if self._refresh_in_progress:
                if not require_fresh and self._cached_keys_are_usable(now):
                    return self._key_set
                deadline = now + self.refresh_wait_timeout
                while self._refresh_in_progress:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise IssuerKeyUnavailableError("Issuer key refresh timed out.")
                    self._refresh_condition.wait(timeout=remaining)
                if self._key_set is None:
                    raise IssuerUnavailableError(
                        "Issuer key refresh is temporarily unavailable."
                    )
                if require_fresh and self._refresh_failed_at:
                    raise IssuerKeyUnavailableError(
                        "Issuer key refresh is temporarily unavailable."
                    )
                return self._serve_stale_or_raise(time.monotonic())
            if self._refresh_failed_at and now - self._refresh_failed_at < min(
                5, self.ttl
            ):
                if self._key_set is not None:
                    if require_fresh:
                        raise IssuerKeyUnavailableError(
                            "Issuer key refresh is temporarily unavailable."
                        )
                    return self._serve_stale_or_raise(now)
                raise IssuerUnavailableError(
                    "Issuer key refresh is temporarily unavailable."
                )
            self._refresh_in_progress = True
            generation = self._refresh_generation
        try:
            key_set, known_kids = self._fetch()
        except IssuerMisconfiguredError:
            # Same reasoning as _fetch: a permanent configuration defect must
            # surface even when a stale key set is cached, not be silently
            # absorbed by the "serve cached key set" fallback below, which
            # would otherwise keep serving stale keys forever behind a
            # config error that will never fix itself on retry. Still runs
            # the same refresh-state cleanup as any other failure.
            with self._refresh_condition:
                self._refresh_failed_at = time.monotonic()
                self._refresh_in_progress = False
                self._refresh_condition.notify_all()
            raise
        except Exception as error:
            with self._refresh_condition:
                self._refresh_failed_at = time.monotonic()
                self._refresh_in_progress = False
                self._refresh_condition.notify_all()
                if self._key_set is not None and isinstance(
                    error,
                    (requests.RequestException, ValueError, AuthError),
                ):
                    if require_fresh:
                        if isinstance(error, IssuerKeyUnavailableError):
                            raise
                        raise IssuerKeyUnavailableError(
                            "Issuer key refresh is temporarily unavailable."
                        ) from error
                    return self._serve_stale_or_raise(time.monotonic(), error)
            raise
        with self._refresh_condition:
            if generation == self._refresh_generation:
                self._key_set = key_set
                self._known_kids = known_kids
                self._fetched_at = time.monotonic()
                self._refresh_generation += 1
            self._refresh_failed_at = 0.0
            self._refresh_in_progress = False
            self._refresh_condition.notify_all()
            return self._key_set

    def get_key_set(self):
        """Return the issuer's key set, refetching only when its TTL expires."""
        with self._lock:
            fresh = (
                self._key_set is not None
                and time.monotonic() - self._fetched_at < self.ttl
            )
            cached = self._key_set
        if fresh:
            return cached
        return self._refresh(wait_for_initial=cached is None)

    def get_cached_key_set(self):
        """Return a fresh local key set without discovery or JWKS network I/O.

        Returning ``None`` for stale state is important for rate limiting: the
        protected endpoint then performs normal validation and refreshes the
        TTL-expired cache instead of reusing rate-limit claims as authentication.
        """
        with self._lock:
            fresh = time.monotonic() - self._fetched_at < self.ttl
            return self._key_set if self._key_set is not None and fresh else None

    def is_unavailable(self):
        """Return whether this cache has no usable key material at all.

        True when the most recent refresh failed and there is no cached key set
        still inside the bounded stale grace period. A cache that has simply
        never been touched yet (fresh boot, no traffic so far) is not reported
        unavailable. Reads existing state only; never triggers a refresh, so
        this is cheap enough to call on every health check.
        """
        with self._lock:
            return bool(self._refresh_failed_at) and not self._cached_keys_are_usable(
                time.monotonic()
            )

    def get_key_set_for_kid(self, kid):
        """Resolve keys for ``kid`` with one guarded refresh opportunity.

        A TTL-expired populated cache is refreshed before inspecting ``kid``.
        When that refresh still does not contain the requested key, record the
        miss without immediately fetching the same JWKS a second time.  A
        fresh cache retains the normal single-flight unknown-key refresh used
        for issuer key rotation.  An empty cache keeps the existing bootstrap
        behavior: fetch current keys, then make one guarded rotation refresh
        if the token already refers to a newer key.
        """
        with self._lock:
            had_cached_keys = self._key_set is not None
            was_stale = (
                not had_cached_keys or time.monotonic() - self._fetched_at >= self.ttl
            )
        key_set = self.get_key_set()
        with self._lock:
            now = time.monotonic()
            self._unknown_kids = {
                missing_kid: expires_at
                for missing_kid, expires_at in self._unknown_kids.items()
                if expires_at > now
            }
            if not kid or kid in self._known_kids:
                return key_set
            if kid in self._unknown_kids:
                if self._refresh_failed_at:
                    raise IssuerKeyUnavailableError(
                        "The token's signing key cannot currently be refreshed."
                    )
                return key_set
            if (
                self._last_unknown_kid_refresh
                and now - self._last_unknown_kid_refresh
                < _MIN_UNKNOWN_KID_REFRESH_INTERVAL
            ):
                # The global backoff suppresses issuer I/O, so a different
                # unseen key is ambiguous: it may be another random key id or
                # a legitimate rotation. Treat it as temporarily unavailable
                # instead of passing a stale set to signature validation,
                # which would turn a recoverable browser session into a
                # terminal 401. Do not retain every key id seen here.
                #
                # When the cache is otherwise healthy (the last refresh
                # attempt succeeded -- not just "we haven't rechecked in
                # 5s"), raise the narrower subclass instead: the
                # bearer-token path treats it as an invalid token (401)
                # rather than an issuer outage (503), since a one-shot API
                # call gains nothing from being told to retry a credential
                # that is, in the common case, simply forged or stale. A
                # failed last refresh keeps the plain (503) classification
                # unchanged -- that ambiguity is real, not just "haven't
                # checked yet."
                error_cls = (
                    IssuerKeyUnavailableError
                    if self._refresh_failed_at
                    else UnknownKeyHealthyBackoffError
                )
                raise error_cls(
                    "The token's signing key cannot currently be refreshed."
                )
            if was_stale and had_cached_keys:
                # The refresh above could not advance the cache, so the issuer
                # is unreachable. Start the global backoff here too, otherwise
                # every request during an outage records another key id.
                self._last_unknown_kid_refresh = now
                self._remember_unknown_kid(kid, now)
                if self._refresh_failed_at:
                    raise IssuerKeyUnavailableError(
                        "The token's signing key cannot currently be refreshed."
                    )
                return key_set
            self._last_unknown_kid_refresh = now
        key_set = self._refresh(wait_for_initial=False, require_fresh=True)
        with self._lock:
            if kid not in self._known_kids:
                self._remember_unknown_kid(kid, time.monotonic())
        return key_set

    def _remember_unknown_kid(self, kid, now):
        """Negative-cache ``kid``, evicting the soonest-expiring entries first.

        Callers hold ``self._lock``.
        """
        self._unknown_kids[kid] = now + _UNKNOWN_KID_TTL
        overflow = len(self._unknown_kids) - _MAX_UNKNOWN_KIDS
        if overflow > 0:
            for expiring_kid in sorted(self._unknown_kids, key=self._unknown_kids.get)[
                :overflow
            ]:
                del self._unknown_kids[expiring_kid]


def _get_jwks_cache():
    """Return this application's lazily-created JWKS cache."""
    cache = current_app.extensions.get(_JWKS_EXTENSION)
    if cache is None:
        auth_config = get_auth_config()
        cache = JWKSCache(
            ttl=auth_config["jwks_ttl"],
            refresh_wait_timeout=max(30, 4 * auth_config["http_timeout"] + 5),
            stale_grace=auth_config["jwks_stale_grace"],
        )
        current_app.extensions[_JWKS_EXTENSION] = cache
    return cache


def jwks_is_unavailable():
    """Return whether this application's JWKS cache has no usable key material.

    Read-only: never triggers a fetch, so safe to call from a health check.
    """
    return _get_jwks_cache().is_unavailable()


def _claims_options():
    auth_config = get_auth_config()
    options = {
        "iss": {"essential": True, "value": auth_config["issuer"]},
        "exp": {"essential": True},
        "sub": {"essential": True},
    }
    options["aud"] = {
        "essential": True,
        # A list of acceptable audiences: authlib's validate_aud() accepts
        # the token when ANY of these is present in its `aud` claim. An
        # empty list would make "values" falsy and skip the check entirely,
        # but that path is unreachable here: whenever an issuer is
        # configured (the only case this function is ever called),
        # discovery.validate_auth_configuration() has already rejected a
        # missing audience at startup.
        "values": auth_config["audience"],
    }
    return options


def _get_token_header(token):
    """Decode and validate the untrusted JWT header without doing any I/O."""
    try:
        encoded_header, _payload, _signature = token.split(".")
        padding = "=" * (-len(encoded_header) % 4)
        header = json.loads(
            base64.urlsafe_b64decode(encoded_header + padding).decode("utf-8")
        )
    except (
        AttributeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise InvalidTokenError(f"Invalid JWT header: {error}")
    if not isinstance(header, dict):
        raise InvalidTokenError("Invalid JWT header.")
    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise InvalidTokenError("JWT signing algorithm is not allowed.")
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise InvalidTokenError("JWT 'kid' header must be a string.")
    return header


def _key_set_for_header(header, allow_remote):
    """Resolve keys for ``header``, optionally fetching from the issuer."""
    cache = _get_jwks_cache()
    if allow_remote:
        return cache.get_key_set_for_kid(header.get("kid"))
    else:
        key_set = cache.get_cached_key_set()
        if key_set is None:
            raise InvalidTokenError("No locally cached issuer keys are available.")
    kid = header.get("kid")
    if kid and kid not in cache._known_kids:
        raise InvalidTokenError("JWT signing key is not locally cached.")
    return key_set


def _decode_token(token, claims_options=None, allow_remote=True):
    """Decode a JWT, refreshing keys only for a non-empty unknown ``kid``."""
    header = _get_token_header(token)
    return _jwt.decode(
        token,
        _key_set_for_header(header, allow_remote),
        claims_options=claims_options,
    )


def validate_access_token(token, allow_remote=True):
    """Validate a JWT access token and return its claims.

    Enforces: signature against the issuer's JWKS (cached, with one guarded
    refetch on unknown ``kid`` to cover key rotation), algorithm allowlist,
    ``iss`` pinned to the configured issuer, ``aud`` containing the
    configured non-empty audience, ``exp``/
    ``nbf`` with the configured leeway, and presence of ``sub``.

    :raises InvalidTokenError: when the token fails any of the above.
    """
    auth_config = get_auth_config()
    if not auth_config["issuer"]:
        raise InvalidTokenError(
            "JWT authentication is not configured (REANA_AUTH_ISSUER unset)."
        )
    claims_options = _claims_options()
    try:
        claims = _decode_token(
            token, claims_options=claims_options, allow_remote=allow_remote
        )
        claims.validate(leeway=auth_config["leeway"])
    except InvalidTokenError:
        raise
    except JoseError as error:
        raise InvalidTokenError(f"Invalid access token: {error}")
    except ValueError as error:
        raise InvalidTokenError(f"Invalid access token: {error}")
    return claims


def validate_id_token(id_token, nonce=None, require_nonce=True):
    """Validate an OIDC ID token returned by the BFF code flow.

    The access token remains the authorization credential; the ID token is
    validated to bind the browser authorization response to the login request
    via ``nonce`` and to catch issuer/client mix-ups early.

    ``require_nonce=False`` is for ID tokens that arrive outside an
    authorization response, such as a refresh response: those carry no nonce
    because there is no matching authorization request to bind them to.
    Signature, issuer, audience, expiry and subject are still enforced.

    :raises InvalidTokenError: when the token is absent or invalid.
    """
    if not id_token:
        raise InvalidTokenError("Issuer did not return an ID token.")
    auth_config = get_auth_config()
    if not auth_config["issuer"]:
        raise InvalidTokenError(
            "OIDC authentication is not configured (REANA_AUTH_ISSUER unset)."
        )
    claims_options = {
        "iss": {"essential": True, "value": auth_config["issuer"]},
        "exp": {"essential": True},
        "sub": {"essential": True},
    }
    if auth_config["web_client_id"]:
        claims_options["aud"] = {
            "essential": True,
            "value": auth_config["web_client_id"],
        }
    try:
        claims = _decode_token(id_token, claims_options=claims_options)
        claims.validate(leeway=auth_config["leeway"])
    except InvalidTokenError:
        raise
    except JoseError as error:
        raise InvalidTokenError(f"Invalid ID token: {error}")
    except ValueError as error:
        raise InvalidTokenError(f"Invalid ID token: {error}")
    if require_nonce and (not nonce or claims.get("nonce") != nonce):
        raise InvalidTokenError("Invalid ID token nonce.")
    return claims


def _as_role_list(value):
    """Normalize an issuer role claim value to a list of strings."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [role for role in value if isinstance(role, str)]
    return []


def extract_roles(claims):
    """Return REANA roles from the configured access-token claim."""
    claim = get_auth_config()["roles_claim"]
    roles = _as_role_list((claims or {}).get(claim))

    # Preserve order for predictable logs/tests while removing duplicates.
    unique_roles = []
    seen = set()
    for role in roles:
        if role not in seen:
            unique_roles.append(role)
            seen.add(role)
    return unique_roles


def require_role(claims):
    """Enforce the configured required role (default ``reana:user``).

    The role replaces the legacy "user has an active token" gate. Roles are
    read exclusively from the configured roles claim (``reana_roles``) in
    the validated access token. UserInfo is identity/profile data and is not
    an authorization source. Authentication-enabled applications require a
    non-empty role at startup.

    :raises MissingRoleError: when the required role is absent.
    """
    required = get_auth_config()["required_role"]
    if not required:
        return
    roles = extract_roles(claims)
    if required not in roles:
        raise MissingRoleError(f"User does not have the required '{required}' role.")
