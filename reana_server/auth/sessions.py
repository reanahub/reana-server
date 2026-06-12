# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Server-side BFF session state.

The BFF keeps exactly one piece of server-side state per browser session:
the issuer's refresh token (plus the id token for RP-initiated logout),
stored in Redis keyed by the issuer session id (``sid`` claim). The access
JWT itself travels in an httpOnly cookie and is validated statelessly; the
refresh token never reaches the browser (AUTH_ARCHITECTURE.md §5.1).
"""

import hmac
import json
import logging
import secrets
import time

import redis
import requests
from flask import request

from reana_server.config import REANA_AUTH
from reana_server.auth import tokens as _tokens
from reana_server.auth.discovery import get_endpoint
from reana_server.auth.errors import InvalidTokenError

AUTH_COOKIE = "reana_at"
"""httpOnly cookie carrying the access JWT."""

CSRF_COOKIE = "reana_csrf"
"""JS-readable cookie for the CSRF double-submit pattern."""

CSRF_HEADER = "X-REANA-CSRF"
"""Header that must echo the CSRF cookie on mutating cookie-auth requests."""

_SESSION_KEY = "reana:bff:session:{sid}"
_LOCK_KEY = "reana:bff:session:{sid}:lock"

_redis_client = None


def get_redis():
    """Return the lazily-created Redis client for BFF session storage."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            REANA_AUTH["redis_url"], decode_responses=True
        )
    return _redis_client


def store_session(sid, refresh_token, id_token="", access_token=""):
    """Persist a BFF session (refresh/id/access token) with TTL."""
    get_redis().setex(
        _SESSION_KEY.format(sid=sid),
        REANA_AUTH["session_ttl"],
        json.dumps(
            {"rt": refresh_token, "idt": id_token, "at": access_token}
        ),
    )


def get_session(sid):
    """Return the stored session dict or ``None``."""
    raw = get_redis().get(_SESSION_KEY.format(sid=sid))
    return json.loads(raw) if raw else None


def delete_session(sid):
    """Delete a stored session."""
    get_redis().delete(_SESSION_KEY.format(sid=sid))


def count_sessions():
    """Count active BFF sessions (for status reporting)."""
    count = 0
    for _ in get_redis().scan_iter(match=_SESSION_KEY.format(sid="*")):
        count += 1
    return count


def decode_expired_token(token):
    """Return claims of a signature-valid but possibly expired token.

    Verifies the signature against the cached JWKS and pins ``iss`` and
    ``aud`` exactly like :func:`reana_server.auth.tokens.
    validate_access_token`, but deliberately skips ``exp``/``nbf``
    validation — used to extract the session id from an expired cookie
    token before attempting a refresh, and at logout.

    :raises InvalidTokenError: on bad signature, issuer or audience.
    """
    cache = _tokens._get_jwks_cache()
    try:
        try:
            claims = _tokens._jwt.decode(token, cache.get_key_set())
        except Exception:
            claims = _tokens._jwt.decode(
                token, cache.get_key_set(force=True)
            )
    except Exception as error:
        raise InvalidTokenError(f"Invalid token: {error}")
    if claims.get("iss") != REANA_AUTH["issuer"]:
        raise InvalidTokenError("Invalid token issuer.")
    audience = REANA_AUTH["audience"]
    if audience:
        aud = claims.get("aud")
        aud = aud if isinstance(aud, list) else [aud]
        if audience not in aud:
            raise InvalidTokenError("Invalid token audience.")
    if not claims.get("sub"):
        raise InvalidTokenError("Token missing 'sub' claim.")
    return claims


def refresh_session(sid):
    """Rotate the session's refresh token; return a fresh access token.

    Guarded by a short Redis lock so concurrent requests (multiple browser
    tabs) do not race the refresh: with refresh-token rotation and reuse
    detection at the issuer, a double refresh would revoke the session.
    The loser of the race polls for the access token the winner stored.

    Returns ``None`` when the session is gone or the issuer rejected the
    refresh (the caller turns this into "please log in again").
    """
    redis_client = get_redis()
    lock_key = _LOCK_KEY.format(sid=sid)
    if not redis_client.set(lock_key, "1", nx=True, ex=10):
        # Another request is refreshing; wait briefly for its result.
        for _ in range(10):
            time.sleep(0.1)
            session = get_session(sid)
            if session and session.get("at"):
                return session["at"]
        return None
    try:
        session = get_session(sid)
        if not session or not session.get("rt"):
            return None
        try:
            response = requests.post(
                get_endpoint("token_url"),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": session["rt"],
                    "client_id": REANA_AUTH["web_client_id"],
                    "client_secret": REANA_AUTH["web_client_secret"],
                },
                timeout=REANA_AUTH["http_timeout"],
            )
        except requests.RequestException as error:
            logging.warning("Token refresh failed (transport): %s", error)
            return None
        if response.status_code == 400:
            # invalid_grant: refresh token revoked/expired at the issuer.
            logging.info("Refresh token rejected by issuer, ending session.")
            delete_session(sid)
            return None
        if response.status_code != 200:
            logging.warning(
                "Token refresh failed with status %s.", response.status_code
            )
            return None
        body = response.json()
        access_token = body["access_token"]
        store_session(
            sid,
            body.get("refresh_token") or session["rt"],
            body.get("id_token") or session.get("idt", ""),
            access_token,
        )
        return access_token
    finally:
        redis_client.delete(lock_key)


def set_auth_cookies(response, access_token):
    """Set the auth cookie (and the CSRF cookie when absent)."""
    response.set_cookie(
        AUTH_COOKIE,
        access_token,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/api",
    )
    if CSRF_COOKIE not in request.cookies:
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
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


def csrf_ok():
    """Check the CSRF double-submit header against the cookie."""
    return hmac.compare_digest(
        request.headers.get(CSRF_HEADER, ""),
        request.cookies.get(CSRF_COOKIE, ""),
    )
