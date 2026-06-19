# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Signed OAuth-state cookies for FastAPI redirect flows.

Shared by the GitLab OAuth connect flow (and available to the BFF login): the
``state`` value plus per-flow payload (e.g. the post-login ``next`` URL) is
stored client-side in a short-lived signed cookie, so reana-server keeps no
server-side state for in-flight authorization redirects.
"""

import hmac
import secrets

from fastapi import HTTPException
from itsdangerous import BadData, URLSafeTimedSerializer

from reana_server.config import SECRET_KEY

STATE_COOKIE = "reana_oauth_state"
STATE_MAX_AGE = 600  # seconds


def _serializer():
    return URLSafeTimedSerializer(SECRET_KEY, salt="reana-oauth-state")


def safe_next_url(target):
    """Return a safe relative redirect target (defaults to ``/``)."""
    if (
        not target
        or not target.startswith("/")
        or target.startswith(("//", "/\\"))
    ):
        return "/"
    return target


def issue_state(response, **payload):
    """Create a random ``state`` and store it (with payload) in the cookie."""
    state = secrets.token_urlsafe(32)
    response.set_cookie(
        STATE_COOKIE,
        _serializer().dumps({"state": state, **payload}),
        max_age=STATE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/api",
    )
    return state


def consume_state(cookie_value, state_param):
    """Validate the returned ``state`` against the cookie; return the payload.

    :raises HTTPException: (403) when missing/expired/tampered or mismatched.
    """
    if not cookie_value or not state_param:
        raise HTTPException(status_code=403, detail="State param is invalid.")
    try:
        data = _serializer().loads(cookie_value, max_age=STATE_MAX_AGE)
    except BadData:
        raise HTTPException(status_code=403, detail="State param is invalid.")
    if not hmac.compare_digest(data.get("state", ""), state_param):
        raise HTTPException(status_code=403, detail="State param is invalid.")
    return data


def clear_state_cookie(response):
    """Delete the state cookie after the round-trip completed."""
    response.delete_cookie(STATE_COOKIE, path="/api")
