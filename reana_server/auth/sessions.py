# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Server-side BFF session state."""

import enum
import hmac
import json
import logging
import math
import secrets
import time

import redis
import requests
from flask import current_app

from reana_server.auth import tokens as _tokens
from reana_server.auth.config import get_auth_config, get_issuer_request_kwargs
from reana_server.auth.discovery import get_endpoint
from reana_server.auth.errors import (
    InvalidTokenError,
    IssuerMisconfiguredError,
    IssuerUnavailableError,
    SessionUnavailableError,
)

_now = time.time

AUTH_COOKIE = "reana_at"
"""httpOnly cookie carrying the access JWT."""

SESSION_COOKIE = "reana_sid"
"""httpOnly cookie carrying the server-generated browser session id."""

CSRF_COOKIE = "reana_csrf"
"""JS-readable cookie for the CSRF double-submit pattern."""

CSRF_HEADER = "X-REANA-CSRF"
"""Header that must echo the CSRF cookie on mutating cookie-auth requests."""

_SESSION_KEY = "reana:bff:session:{sid}"
_LOCK_KEY = "reana:bff:session:{sid}:lock"

_REDIS_EXTENSION = "reana_auth_redis"


class RefreshOutcome(enum.Enum):
    """Possible outcomes of a browser-session refresh."""

    SUCCESS = "success"
    TERMINAL = "terminal"
    TRANSIENT = "transient"


class RefreshResult:
    """Result of refreshing a browser session without conflating failures."""

    def __init__(self, outcome, access_token=None):
        """Store the classified outcome and optional refreshed access token."""
        self.outcome = outcome
        self.access_token = access_token


def _session_unavailable(error):
    """Convert Redis failures into a safe, service-level auth error."""
    logging.warning("BFF session storage is unavailable: %s", error)
    return SessionUnavailableError(
        "Authentication session service is temporarily unavailable."
    )


def get_redis():
    """Return the lazily-created Redis client for BFF session storage."""
    client = current_app.extensions.get(_REDIS_EXTENSION)
    if client is None:
        client = redis.Redis.from_url(
            get_auth_config()["redis_url"],
            decode_responses=True,
            socket_connect_timeout=get_auth_config()["redis_socket_connect_timeout"],
            socket_timeout=get_auth_config()["redis_socket_timeout"],
            health_check_interval=get_auth_config()["redis_health_check_interval"],
        )
        current_app.extensions[_REDIS_EXTENSION] = client
    return client


def store_session(
    sid,
    refresh_token,
    id_token="",
    access_token="",
    *,
    issuer,
    subject,
    client_id,
    created_at,
    require_existing=False,
):
    """Persist a BFF session bound to one issuer, subject, and client.

    ``created_at`` is the session's original creation time (a Unix
    timestamp), supplied by the caller rather than defaulted here: a fresh
    login passes ``time.time()``, while a refresh
    (:func:`_refresh_locked_session`) must forward the *existing* session's
    ``created_at`` unchanged. Without that distinction, re-arming the full
    ``session_ttl`` on every refresh would let an actively used session's
    expiry slide forward indefinitely, defeating the documented absolute
    cap ("removed after REANA_AUTH_SESSION_TTL even if the issuer would
    keep [refresh tokens] longer"). The TTL actually written is instead
    bounded by how much of that original window remains; a session whose
    window has already elapsed is deleted rather than re-armed.

    ``require_existing`` makes the write a replace-only ``SET ... XX``
    instead of an unconditional one, returning ``False`` (instead of
    creating the key) if it is currently absent. A fresh login must NOT
    pass this -- it needs to create a brand-new key. A refresh
    (:func:`_refresh_locked_session`) MUST pass ``require_existing=True``:
    the refresh lock it holds only serializes concurrent refreshes of the
    *same* session against each other, never against
    :func:`delete_session`/:func:`delete_sessions_for_subject` (neither
    touches the lock). Without this, a session deleted by a logout or an
    admin's ``revoke-identity`` while a refresh is already in flight would
    get silently recreated by this function's own write once the refresh's
    issuer round-trip completes -- resurrecting a session that was just
    revoked, with a freshly rotated refresh token. Returns whether the key
    was actually written.
    """
    session_ttl = get_auth_config()["session_ttl"]
    remaining_ttl = math.ceil(created_at + session_ttl - _now())
    try:
        if remaining_ttl <= 0:
            get_redis().delete(_SESSION_KEY.format(sid=sid))
            return False
        stored = get_redis().set(
            _SESSION_KEY.format(sid=sid),
            json.dumps(
                {
                    "rt": refresh_token,
                    "idt": id_token,
                    "at": access_token,
                    "iss": issuer,
                    "sub": subject,
                    "cid": client_id,
                    "created_at": created_at,
                }
            ),
            ex=min(session_ttl, remaining_ttl),
            xx=require_existing,
        )
        return bool(stored)
    except redis.RedisError as error:
        raise _session_unavailable(error) from error


def get_session(sid):
    """Return the stored session dict or ``None``."""
    try:
        raw = get_redis().get(_SESSION_KEY.format(sid=sid))
    except redis.RedisError as error:
        raise _session_unavailable(error) from error
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logging.warning("Discarding malformed BFF session %s.", sid)
        delete_session(sid)
        return None


def delete_session(sid):
    """Delete a stored session."""
    try:
        get_redis().delete(_SESSION_KEY.format(sid=sid))
    except redis.RedisError as error:
        raise _session_unavailable(error) from error


_COUNT_SESSIONS_CACHE_TTL = 5
"""How long a session count is reused before re-scanning Redis, in seconds.

/api/status is reachable by any signed-in user, and each call would
otherwise trigger a full Redis SCAN over every session key -- bounding
that cost here is cheaper and lower-risk than adding this codebase's
first admin-gated REST endpoint just to restrict who can trigger it.
"""
_count_sessions_cache = {"value": 0, "at": 0.0}


def count_sessions():
    """Count active BFF sessions (for status reporting)."""
    now = time.monotonic()
    if now - _count_sessions_cache["at"] < _COUNT_SESSIONS_CACHE_TTL:
        return _count_sessions_cache["value"]
    try:
        keys = get_redis().scan_iter(match=_SESSION_KEY.format(sid="*"))
        count = sum(not key.endswith(":lock") for key in keys)
    except redis.RedisError as error:
        raise _session_unavailable(error) from error
    _count_sessions_cache["value"] = count
    _count_sessions_cache["at"] = now
    return count


def delete_sessions_for_subject(issuer, subject, *, dry_run=False):
    """Delete every BFF session bound to one issuer/subject identity.

    Unlike interactive sessions and the GitLab webhook secret, BFF sessions
    are keyed by a random ``sid``, not by owner -- there is no indexed
    lookup path by identity. A full scan is used instead: this is an
    admin-invoked, identity-targeted revocation, not a hot path, and the
    live BFF session count is bounded by concurrently signed-in browsers,
    so the O(live sessions) cost of scanning is an accepted, deliberate
    tradeoff rather than a gap to close with new persistent infrastructure.

    :param dry_run: report the count that would be deleted without deleting.
    :return: number of matching sessions found (deleted unless ``dry_run``).
    """
    try:
        redis_client = get_redis()
        keys = redis_client.scan_iter(match=_SESSION_KEY.format(sid="*"))
        matched = 0
        for key in keys:
            if key.endswith(":lock"):
                continue
            raw = redis_client.get(key)
            if not raw:
                continue
            try:
                session = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not session_matches_identity(session, issuer, subject):
                continue
            matched += 1
            if not dry_run:
                redis_client.delete(key)
        return matched
    except redis.RedisError as error:
        raise _session_unavailable(error) from error


def decode_expired_token(token):
    """Return claims of a signature-valid but possibly expired token.

    Verifies the signature against the cached JWKS and pins ``iss`` and
    ``aud`` exactly like :func:`reana_server.auth.tokens.
    validate_access_token`, but deliberately skips ``exp``/``nbf``
    validation. This ensures an expired cookie is still authentic before a
    refresh is attempted using the separate browser-session cookie.

    :raises InvalidTokenError: on bad signature, issuer or audience.
    :raises IssuerUnavailableError: when the issuer's JWKS cannot currently
        be fetched -- a transient, potentially self-healing state, distinct
        from a definitively invalid token.
    :raises IssuerMisconfiguredError: when the issuer integration itself is
        misconfigured.
    """
    try:
        claims = _tokens._decode_token(token)
    except InvalidTokenError:
        raise
    except (IssuerUnavailableError, IssuerMisconfiguredError):
        # Not a token-validity verdict: re-raise as-is so the caller (see
        # decorators.py's _authenticate) can tell an issuer outage apart
        # from a definitively bad cookie and avoid clearing cookies over a
        # condition that may resolve on its own. Same classification
        # already applied to _fetch_discovery_document/JWKSCache._fetch;
        # this call site just postdates that fix.
        raise
    except Exception as error:
        logging.exception("Unexpected error while decoding an expired access token.")
        raise InvalidTokenError(f"Invalid token: {error}")
    auth_config = get_auth_config()
    if claims.get("iss") != auth_config["issuer"]:
        raise InvalidTokenError("Invalid token issuer.")
    audiences = auth_config["audience"]
    if audiences:
        aud = claims.get("aud")
        aud = aud if isinstance(aud, list) else [aud]
        if not any(audience in aud for audience in audiences):
            raise InvalidTokenError("Invalid token audience.")
    if not claims.get("sub"):
        raise InvalidTokenError("Token missing 'sub' claim.")
    return claims


def _redis_set_refresh_lock(redis_client, lock_key, lock_ttl):
    """Acquire the per-session refresh lock and return its ownership token."""
    lock_owner = secrets.token_urlsafe(32)
    try:
        acquired = redis_client.set(lock_key, lock_owner, nx=True, ex=lock_ttl)
    except redis.RedisError as error:
        raise _session_unavailable(error) from error
    return lock_owner if acquired else None


def _refresh_lock_ttl(auth_config):
    """Return a bounded lock TTL covering discovery, refresh, and Redis I/O."""
    worst_case = (
        # requests applies a scalar timeout independently to connect and read;
        # allow both phases for discovery and for the token refresh request.
        (4 * auth_config["http_timeout"])
        + (2 * auth_config["redis_socket_timeout"])
        + (2 * auth_config["redis_socket_connect_timeout"])
        + 5
    )
    return max(60, math.ceil(worst_case))


def _wait_for_concurrent_refresh(
    redis_client,
    sid,
    lock_key,
    wait_timeout,
    previous_access_token,
    expected_issuer,
    expected_subject,
):
    """Wait for another request's refresh and return its stored access token."""
    deadline = time.monotonic() + max(0.1, wait_timeout)
    while time.monotonic() < deadline:
        time.sleep(0.1)
        session = get_session(sid)
        if session and not session_matches_identity(
            session, expected_issuer, expected_subject
        ):
            return RefreshResult(RefreshOutcome.TERMINAL)
        if session and session.get("at") and session["at"] != previous_access_token:
            return RefreshResult(RefreshOutcome.SUCCESS, session["at"])
        try:
            if not redis_client.exists(lock_key):
                break
        except redis.RedisError as error:
            raise _session_unavailable(error) from error
    session = get_session(sid)
    if session and not session_matches_identity(
        session, expected_issuer, expected_subject
    ):
        return RefreshResult(RefreshOutcome.TERMINAL)
    if session and session.get("at") and session["at"] != previous_access_token:
        return RefreshResult(RefreshOutcome.SUCCESS, session["at"])
    if not session:
        return RefreshResult(RefreshOutcome.TERMINAL)
    return RefreshResult(RefreshOutcome.TRANSIENT)


def _request_refreshed_tokens(refresh_token, auth_config):
    """Call the issuer and return ``(outcome, token_body_or_none)``."""
    try:
        response = requests.post(
            get_endpoint("token_url"),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": auth_config["web_client_id"],
                "client_secret": auth_config["web_client_secret"],
            },
            **get_issuer_request_kwargs(),
        )
    except requests.RequestException as error:
        logging.warning("Token refresh failed (transport): %s", error)
        return RefreshOutcome.TRANSIENT, None
    if response.status_code != 200:
        try:
            error_code = response.json().get("error")
        except (ValueError, AttributeError):
            error_code = None
        if error_code == "invalid_grant":
            return RefreshOutcome.TERMINAL, None
        logging.warning("Token refresh failed with status %s.", response.status_code)
        return RefreshOutcome.TRANSIENT, None
    try:
        body = response.json()
        body["access_token"]
    except (TypeError, ValueError, KeyError) as error:
        logging.warning("Token refresh returned an invalid response: %s", error)
        return RefreshOutcome.TRANSIENT, None
    return RefreshOutcome.SUCCESS, body


def session_matches_identity(session, expected_issuer, expected_subject):
    """Return whether stored session state belongs to the cookie identity."""
    return (
        session.get("iss") == expected_issuer and session.get("sub") == expected_subject
    )


def _refresh_locked_session(sid, auth_config, expected_issuer, expected_subject):
    """Refresh a session while the caller holds its Redis lock."""
    session = get_session(sid)
    if not session or not session.get("rt"):
        return RefreshResult(RefreshOutcome.TERMINAL)
    if (
        session.get("iss") != auth_config["issuer"]
        or session.get("cid") != auth_config["web_client_id"]
        or not session.get("sub")
    ):
        logging.warning("Ending a browser session with invalid identity binding.")
        delete_session(sid)
        return RefreshResult(RefreshOutcome.TERMINAL)
    if not session_matches_identity(session, expected_issuer, expected_subject):
        # Clear only the mismatched cookies in the current response.  Do not
        # delete the referenced session: it may be a valid session belonging
        # to another browser identity.
        logging.warning("Rejecting mismatched browser access/session cookies.")
        return RefreshResult(RefreshOutcome.TERMINAL)
    created_at = session.get("created_at", _now())
    if created_at + auth_config["session_ttl"] <= _now():
        delete_session(sid)
        return RefreshResult(RefreshOutcome.TERMINAL)
    outcome, body = _request_refreshed_tokens(session["rt"], auth_config)
    if outcome is RefreshOutcome.TERMINAL:
        logging.info("Refresh token rejected by issuer, ending session.")
        try:
            delete_session(sid)
        except SessionUnavailableError:
            # The browser is still logged out locally by the caller; the
            # inaccessible Redis entry expires by its own TTL.
            logging.warning("Could not delete a rejected BFF session.")
        return RefreshResult(RefreshOutcome.TERMINAL)
    if outcome is RefreshOutcome.TRANSIENT:
        return RefreshResult(RefreshOutcome.TRANSIENT)
    access_token = body["access_token"]
    try:
        claims = _tokens.validate_access_token(access_token)
    except InvalidTokenError as error:
        logging.warning("Issuer returned an invalid refreshed access token: %s", error)
        delete_session(sid)
        return RefreshResult(RefreshOutcome.TERMINAL)
    if claims.get("iss") != session["iss"] or claims.get("sub") != session["sub"]:
        logging.warning("Refreshed access token changed browser-session identity.")
        delete_session(sid)
        return RefreshResult(RefreshOutcome.TERMINAL)
    try:
        refreshed_id_token = _refreshed_id_token(body.get("id_token"), session)
    except InvalidTokenError as error:
        logging.warning(
            "Refreshed ID token changed browser-session identity: %s", error
        )
        delete_session(sid)
        return RefreshResult(RefreshOutcome.TERMINAL)
    stored = store_session(
        sid,
        body.get("refresh_token") or session["rt"],
        refreshed_id_token,
        access_token,
        issuer=session["iss"],
        subject=session["sub"],
        client_id=session["cid"],
        # A session stored before this field existed has no recorded
        # creation time; treat it as created now rather than raising, so
        # refreshing it is not silently blocked -- it still receives the
        # bounded-cap treatment from that point forward.
        created_at=created_at,
        # Replace-only: a logout or admin revoke-identity racing this
        # refresh (neither coordinates with the lock this function holds)
        # may have already deleted the session. Without require_existing,
        # this write would silently recreate it once the issuer round-trip
        # above completes -- resurrecting a session that was just revoked.
        # A narrower window remains between this write succeeding and this
        # function returning, both still inside the lock: a revocation
        # landing there still deletes the key correctly, but the
        # already-in-flight caller walks away with one legitimately-issued,
        # short-lived access token. That's the same already-documented JWT
        # non-revocability caveat as everywhere else in this codebase
        # (revoke_identity's own docstring: "a live access token keeps
        # working until it expires regardless of anything this command
        # does"), not a new exposure -- not worth closing with a blocking
        # lock acquisition in the deleter, which would turn an admin-facing
        # revocation command into something that can hang for a lock's
        # full TTL per session scanned.
        require_existing=True,
    )
    if not stored:
        logging.info(
            "Refreshed session could not be persisted (absolute TTL "
            "elapsed or session was concurrently revoked); ending session."
        )
        return RefreshResult(RefreshOutcome.TERMINAL)
    return RefreshResult(RefreshOutcome.SUCCESS, access_token)


def _refreshed_id_token(id_token, session):
    """Return the ID token to keep as this session's RP-initiated logout hint.

    The ID token is never an authorization credential here; its only use is
    ``id_token_hint`` at logout, and issuers can reject a hint that has since
    expired. OIDC permits a refresh response to omit the ID token, in which
    case the login-time hint is retained. If a new ID token is supplied, it is
    validated without a nonce and must remain pinned to the session identity.
    """
    previous_id_token = session.get("idt", "")
    if not id_token:
        return previous_id_token
    try:
        claims = _tokens.validate_id_token(id_token, require_nonce=False)
    except InvalidTokenError as error:
        raise InvalidTokenError(f"Invalid refreshed ID token: {error}") from error
    if claims.get("iss") != session["iss"] or claims.get("sub") != session["sub"]:
        raise InvalidTokenError(
            "Refreshed ID token identity differs from the browser session."
        )
    return id_token


def _release_refresh_lock(redis_client, lock_key, lock_owner):
    """Atomically release a refresh lock only when ``lock_owner`` still owns it."""
    try:
        with redis_client.pipeline() as pipeline:
            pipeline.watch(lock_key)
            if pipeline.get(lock_key) != lock_owner:
                pipeline.unwatch()
                return False
            pipeline.multi()
            pipeline.delete(lock_key)
            (deleted,) = pipeline.execute()
            return bool(deleted)
    except redis.WatchError:
        # The lock expired or changed ownership between WATCH and EXEC.
        return False
    except redis.RedisError as error:
        raise _session_unavailable(error) from error


def refresh_session(
    sid, previous_access_token="", *, expected_issuer, expected_subject
):
    """Rotate the session refresh token and return a three-state result.

    Guarded by a short Redis lock so concurrent requests (multiple browser
    tabs) do not race the refresh: with refresh-token rotation and reuse
    detection at the issuer, a double refresh would revoke the session.
    The loser of the race polls for the access token the winner stored.

    The caller supplies the issuer and subject from the signature-validated
    expired access cookie.  They must match the Redis session before either a
    refresh request or a concurrent winner's token can be accepted.

    ``TERMINAL`` means the session is absent, belongs to a different cookie
    identity, or the issuer definitively rejected its refresh token.
    ``TRANSIENT`` covers transport, issuer, lock, and malformed-response
    failures for which cookies and session state must be preserved. Redis
    failures raise :class:`SessionUnavailableError`.
    """
    redis_client = get_redis()
    lock_key = _LOCK_KEY.format(sid=sid)
    auth_config = get_auth_config()
    lock_ttl = _refresh_lock_ttl(auth_config)
    lock_owner = _redis_set_refresh_lock(redis_client, lock_key, lock_ttl)
    if not lock_owner:
        return _wait_for_concurrent_refresh(
            redis_client,
            sid,
            lock_key,
            auth_config["refresh_wait_timeout"],
            previous_access_token,
            expected_issuer,
            expected_subject,
        )
    try:
        return _refresh_locked_session(
            sid, auth_config, expected_issuer, expected_subject
        )
    finally:
        _release_refresh_lock(redis_client, lock_key, lock_owner)


def set_auth_cookies(response, access_token, session_id=None, existing_cookies=None):
    """Set the auth cookie (and the CSRF cookie when absent).

    :param existing_cookies: mapping of cookies already on the request, used
        to avoid rotating the CSRF cookie (empty means "always set it").
    """
    existing_cookies = existing_cookies or {}
    response.set_cookie(
        AUTH_COOKIE,
        access_token,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/api",
    )
    if session_id:
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/api",
        )
    if CSRF_COOKIE not in existing_cookies:
        response.set_cookie(
            CSRF_COOKIE,
            secrets.token_urlsafe(32),
            secure=True,
            samesite="Lax",
            path="/",
        )
    return response


def clear_auth_cookies(response):
    """Delete the auth and CSRF cookies."""
    response.delete_cookie(AUTH_COOKIE, path="/api")
    response.delete_cookie(SESSION_COOKIE, path="/api")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


def csrf_ok(headers, cookies):
    """Check the CSRF double-submit header against the cookie.

    ``headers``/``cookies`` are explicit request mappings.
    """
    header_value = headers.get(CSRF_HEADER, "")
    cookie_value = cookies.get(CSRF_COOKIE, "")
    # A missing token must fail closed: ``compare_digest("", "")`` is True, so
    # require a non-empty header that matches the cookie.
    return bool(header_value) and hmac.compare_digest(header_value, cookie_value)
