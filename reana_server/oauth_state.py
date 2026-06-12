# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Signed OAuth state cookies.

Shared by the BFF login flow and the GitLab OAuth connect flow: the
``state`` parameter (plus per-flow payload such as the PKCE verifier and
the post-login redirect target) is stored client-side in a short-lived
signed cookie, so reana-server needs no server-side state for in-flight
authorization redirects.
"""

import hmac
import secrets

from flask import current_app, request
from itsdangerous import BadData, URLSafeTimedSerializer

STATE_COOKIE = "reana_oauth_state"
STATE_MAX_AGE = 600  # seconds


class InvalidOAuthState(Exception):
    """The OAuth state is missing, tampered with, or expired (HTTP 403)."""


def _serializer():
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"], salt="reana-oauth-state"
    )


def safe_next_url(target):
    """Return a safe relative redirect target (defaults to ``/``)."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


def issue_state(response, **payload):
    """Create a random ``state`` and store it with payload in a cookie.

    :param response: response the state cookie is set on (the OAuth
        redirect response).
    :param payload: additional values to carry through the round-trip
        (e.g. PKCE ``verifier``, ``next`` URL).
    :return: the ``state`` value to put in the authorization URL.
    """
    state = secrets.token_urlsafe(32)
    cookie_value = _serializer().dumps({"state": state, **payload})
    response.set_cookie(
        STATE_COOKIE,
        cookie_value,
        max_age=STATE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/api",
    )
    return state


def consume_state(state_param):
    """Validate the returned ``state`` against the cookie; return payload.

    :raises InvalidOAuthState: when the cookie is missing/expired/tampered
        or the state value does not match.
    """
    cookie_value = request.cookies.get(STATE_COOKIE)
    if not cookie_value or not state_param:
        raise InvalidOAuthState("State param is invalid.")
    try:
        data = _serializer().loads(cookie_value, max_age=STATE_MAX_AGE)
    except BadData:
        raise InvalidOAuthState("State param is invalid.")
    if not hmac.compare_digest(data.get("state", ""), state_param):
        raise InvalidOAuthState("State param is invalid.")
    return data


def clear_state_cookie(response):
    """Delete the state cookie after the round-trip completed."""
    response.delete_cookie(STATE_COOKIE, path="/api")
    return response
