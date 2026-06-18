# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Authentication endpoints: OIDC discovery relay + BFF browser login.

``/.well-known/openid-configuration`` relays the trusted issuer's metadata
(+ the REANA CLI client id) so clients only need the REANA URL. The BFF flow
(``/login`` → issuer → ``/oauth/callback`` → ``/logout``) runs the
authorization-code + PKCE flow server-side and gives browsers an httpOnly
cookie session; the refresh token never reaches the browser
(``auth_contract_freeze.md`` §Browser Session).
"""

import base64
import hashlib
import hmac
import logging
import secrets
from urllib.parse import urlparse, urlunparse, urlencode

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadData, URLSafeTimedSerializer

from reana_server.auth import (
    MissingRoleError,
    ProvisioningError,
    get_or_provision_user,
    validate_access_token,
)
from reana_server.auth.discovery import get_endpoint, get_openid_configuration
from reana_server.auth.errors import AuthError, InvalidTokenError
from reana_server.auth.sessions import (
    AUTH_COOKIE,
    clear_auth_cookies,
    csrf_ok,
    decode_expired_token,
    delete_session,
    get_session,
    set_auth_cookies,
    store_session,
)
from reana_server.auth.userinfo import fetch_userinfo
from reana_server.config import REANA_AUTH, REANA_URL, SECRET_KEY
from reana_server.groups.sync import sync_user_groups_from_userinfo

router = APIRouter(tags=["auth"])

STATE_COOKIE = "reana_oauth_state"
STATE_MAX_AGE = 600  # seconds


def _serializer():
    return URLSafeTimedSerializer(SECRET_KEY, salt="reana-oauth-state")


def _safe_next_url(target):
    """Return a safe relative redirect target (defaults to ``/``)."""
    if (
        not target
        or not target.startswith("/")
        or target.startswith(("//", "/\\"))
    ):
        return "/"
    return target


def _bff_active():
    return bool(REANA_AUTH["bff_enabled"] and REANA_AUTH["issuer"])


def _callback_redirect_uri():
    return f"{REANA_URL}/api/oauth/callback"


def _require_bff():
    if not _bff_active():
        raise HTTPException(status_code=404, detail="Browser login is not enabled.")
    missing = []
    if not SECRET_KEY:
        missing.append("REANA_SECRET_KEY")
    if not REANA_AUTH["web_client_id"]:
        missing.append("REANA_AUTH_WEB_CLIENT_ID")
    if not REANA_AUTH["web_client_secret"]:
        missing.append("REANA_AUTH_WEB_CLIENT_SECRET")
    if missing:
        raise HTTPException(
            status_code=503,
            detail="Browser login is misconfigured; missing "
            + ", ".join(missing)
            + ".",
        )
    try:
        get_endpoint("authorization_url")
        get_endpoint("token_url")
    except AuthError as error:
        raise HTTPException(
            status_code=503,
            detail=f"Browser login is misconfigured: {error}",
        )


def _client_facing_endpoint_url(endpoint_url):
    """Return endpoint URL rewritten to the public issuer host when possible."""
    issuer = REANA_AUTH["issuer"]
    if not issuer or not endpoint_url:
        return endpoint_url
    issuer_parts = urlparse(issuer)
    endpoint_parts = urlparse(endpoint_url)
    if not issuer_parts.scheme or not issuer_parts.netloc:
        return endpoint_url
    if not endpoint_parts.scheme or not endpoint_parts.netloc:
        return endpoint_url
    issuer_path = issuer_parts.path.rstrip("/")
    if issuer_path and not endpoint_parts.path.startswith(issuer_path + "/"):
        return endpoint_url
    return urlunparse(
        (
            issuer_parts.scheme,
            issuer_parts.netloc,
            endpoint_parts.path,
            endpoint_parts.params,
            endpoint_parts.query,
            endpoint_parts.fragment,
        )
    )


def _client_facing_openid_configuration(document):
    """Return discovery document suitable for host/browser-side clients."""
    public_document = dict(document)
    if REANA_AUTH["issuer"]:
        public_document["issuer"] = REANA_AUTH["issuer"]
    for field in (
        "authorization_endpoint",
        "token_endpoint",
        "userinfo_endpoint",
        "jwks_uri",
        "end_session_endpoint",
        "device_authorization_endpoint",
    ):
        if field in public_document:
            public_document[field] = _client_facing_endpoint_url(
                public_document[field]
            )
    return public_document


def _consume_state(cookie_value, state_param):
    if not cookie_value or not state_param:
        raise HTTPException(status_code=403, detail="State param is invalid.")
    try:
        data = _serializer().loads(cookie_value, max_age=STATE_MAX_AGE)
    except BadData:
        raise HTTPException(status_code=403, detail="State param is invalid.")
    if not hmac.compare_digest(data.get("state", ""), state_param):
        raise HTTPException(status_code=403, detail="State param is invalid.")
    return data


@router.get(
    "/.well-known/openid-configuration",
    summary="Relay the trusted issuer's OIDC metadata + REANA CLI client id",
)
async def openid_configuration() -> JSONResponse:
    """Return the issuer discovery document plus the REANA CLI client id."""
    try:
        document = _client_facing_openid_configuration(get_openid_configuration())
    except AuthError as error:
        raise HTTPException(status_code=503, detail=str(error))
    document["reana_cli_client_id"] = REANA_AUTH["cli_client_id"]
    document["reana_client_id"] = REANA_AUTH["cli_client_id"]  # legacy alias
    return JSONResponse(content=document)


@router.get("/login", summary="Start the browser login flow (BFF)")
async def login(request: Request, next: str = "/") -> RedirectResponse:
    """Redirect to the issuer's authorization endpoint (code flow + PKCE)."""
    _require_bff()
    next_url = _safe_next_url(next)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    try:
        authorization_url = get_endpoint("authorization_url")
    except AuthError:
        raise HTTPException(
            status_code=502, detail="Could not reach the identity provider."
        )
    state = secrets.token_urlsafe(32)
    cookie_value = _serializer().dumps(
        {"state": state, "verifier": verifier, "next": next_url}
    )
    location = authorization_url + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": REANA_AUTH["web_client_id"],
            "redirect_uri": _callback_redirect_uri(),
            "scope": REANA_AUTH["scopes"],
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    response = RedirectResponse(url=location, status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        cookie_value,
        max_age=STATE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/api",
    )
    return response


def _sync_groups_bg(user, access_token: str) -> None:
    """Sync group memberships after the login redirect has already been sent."""
    try:
        sync_user_groups_from_userinfo(user, fetch_userinfo(access_token))
    except Exception:
        logging.exception("Background group sync failed for user %s.", user.id_)


@router.get("/oauth/callback", summary="Complete the browser login flow (BFF)")
async def oauth_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    code: str = "",
    state: str = "",
    error: str = "",
) -> RedirectResponse:
    """Exchange the code, provision the user, sync groups, set the cookie."""
    _require_bff()
    state_data = _consume_state(request.cookies.get(STATE_COOKIE), state)
    next_url = _safe_next_url(state_data.get("next"))
    if error:
        logging.warning("Issuer returned an authorization error: %s", error)
        response = RedirectResponse(
            f"{next_url}?login_error=authorization", status_code=302
        )
        response.delete_cookie(STATE_COOKIE, path="/api")
        return response
    try:
        token_response = requests.post(
            get_endpoint("token_url"),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _callback_redirect_uri(),
                "code_verifier": state_data.get("verifier", ""),
                "client_id": REANA_AUTH["web_client_id"],
                "client_secret": REANA_AUTH["web_client_secret"],
            },
            timeout=REANA_AUTH["http_timeout"],
        )
        logging.info(token_response.elapsed.total_seconds())
        token_response.raise_for_status()
        token_body = token_response.json()
        access_token = token_body["access_token"]
    except (requests.RequestException, ValueError, KeyError) as error:
        logging.error("Authorization code exchange failed: %s", error)
        raise HTTPException(
            status_code=502, detail="Token exchange with the issuer failed."
        )
    try:
        claims = validate_access_token(access_token)
    except InvalidTokenError as error:
        logging.error("Issuer returned an invalid access token: %s", error)
        raise HTTPException(
            status_code=502, detail="Token exchange with the issuer failed."
        )

    try:
        user, is_new = get_or_provision_user(claims, access_token)
        if not is_new:
            # New users had groups synced synchronously inside provisioning.
            # For returning users, re-sync after the redirect so login latency
            # is not affected by the Admin API / userinfo round-trips.
            background_tasks.add_task(_sync_groups_bg, user, access_token)
    except MissingRoleError:
        # Session is still established; /api/you answers 403 and the UI shows
        # the "access not granted" state.
        logging.info("User without the required role logged in via BFF.")
    except ProvisioningError as error:
        logging.warning("Could not provision user at login: %s", error)
        response = RedirectResponse(
            f"{next_url}?login_error=provisioning", status_code=302
        )
        response.delete_cookie(STATE_COOKIE, path="/api")
        return response

    sid = claims.get("sid") or claims["sub"]
    store_session(
        sid,
        token_body.get("refresh_token", ""),
        token_body.get("id_token", ""),
        access_token,
    )
    response = RedirectResponse(next_url, status_code=302)
    set_auth_cookies(response, access_token, existing_cookies=request.cookies)
    response.delete_cookie(STATE_COOKIE, path="/api")
    return response


@router.post("/logout", summary="End the browser session (BFF)")
async def logout(request: Request) -> JSONResponse:
    """Delete the server session, clear cookies, return issuer logout URL."""
    _require_bff()
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="User not signed in.")
    if not csrf_ok(request.headers, request.cookies):
        raise HTTPException(
            status_code=403, detail="CSRF token missing or invalid."
        )
    logout_url = ""
    try:
        claims = decode_expired_token(token)
        sid = claims.get("sid") or claims["sub"]
        session_data = get_session(sid)
        delete_session(sid)
        params = {
            "post_logout_redirect_uri": REANA_URL,
            "client_id": REANA_AUTH["web_client_id"],
        }
        if session_data and session_data.get("idt"):
            params["id_token_hint"] = session_data["idt"]
        logout_url = get_endpoint("end_session_url") + "?" + urlencode(params)
    except (InvalidTokenError, AuthError) as error:
        logging.info("Logout with unusable session token: %s", error)
    response = JSONResponse({"logout_url": logout_url})
    clear_auth_cookies(response)
    return response
