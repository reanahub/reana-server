# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server auth Flask-Blueprint."""

import base64
import hashlib
import logging
import secrets
from urllib.parse import urlencode

import requests
from flask import Blueprint, jsonify, redirect, request

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
from reana_server.config import REANA_AUTH, REANA_URL
from reana_server.groups.sync import sync_user_groups_from_userinfo
from reana_server.oauth_state import (
    InvalidOAuthState,
    clear_state_cookie,
    consume_state,
    issue_state,
    safe_next_url,
)

blueprint = Blueprint("auth", __name__)


def _bff_active():
    return bool(REANA_AUTH["bff_enabled"] and REANA_AUTH["issuer"])


def _callback_redirect_uri():
    return f"{REANA_URL}/api/oauth/callback"


@blueprint.route("/.well-known/openid-configuration", methods=["GET"])
def openid_configuration():
    r"""Get the trusted issuer's OpenID configuration.

    ---
    get:
      summary: Get the trusted issuer's OpenID configuration.
      description: >-
        Relays the OIDC discovery document of the deployment's trusted
        issuer, extended with the public client id that reana-client must
        use for the device authorization grant. This lets clients discover
        the identity provider knowing only the REANA URL.
      operationId: get_openid_configuration
      produces:
        - application/json
      responses:
        200:
          description: >-
            Request succeeded. The response contains the issuer's OpenID
            configuration and the REANA CLI client id.
          schema:
            type: object
            properties:
              issuer:
                type: string
              device_authorization_endpoint:
                type: string
              authorization_endpoint:
                type: string
              token_endpoint:
                type: string
              userinfo_endpoint:
                type: string
              jwks_uri:
                type: string
              reana_client_id:
                type: string
        502:
          description: >-
            Request failed. The issuer's OpenID configuration could not be
            fetched.
          schema:
            type: object
            properties:
              message:
                type: string
    """
    try:
        configuration = dict(get_openid_configuration())
    except AuthError as error:
        logging.error("Could not relay OpenID configuration: %s", error)
        return (
            jsonify(message="Could not fetch the issuer's OpenID configuration."),
            502,
        )
    configuration["reana_client_id"] = REANA_AUTH["cli_client_id"]
    return jsonify(configuration), 200


@blueprint.route("/login", methods=["GET"])
def login():
    r"""Start the browser login flow (BFF).

    ---
    get:
      summary: Start the browser login flow.
      description: >-
        Redirects the browser to the trusted issuer's authorization
        endpoint (authorization code flow with PKCE). On completion the
        issuer redirects back to the OAuth callback, which establishes the
        cookie-based session. Returns 404 when the BFF login is disabled.
      operationId: bff_login
      parameters:
        - name: next
          in: query
          description: Relative URL to return to after login.
          required: false
          type: string
      responses:
        302:
          description: Redirect to the issuer's authorization endpoint.
        404:
          description: BFF login is not enabled on this deployment.
        502:
          description: The issuer's endpoints could not be resolved.
    """
    if not _bff_active():
        return jsonify(message="Browser login is not enabled."), 404
    next_url = safe_next_url(request.args.get("next"))
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    try:
        authorization_url = get_endpoint("authorization_url")
    except AuthError as error:
        logging.error("Could not resolve authorization endpoint: %s", error)
        return jsonify(message="Could not reach the identity provider."), 502
    response = redirect("placeholder")
    state = issue_state(response, verifier=verifier, next=next_url)
    response.headers["Location"] = (
        authorization_url
        + "?"
        + urlencode(
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
    )
    return response


@blueprint.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    r"""Complete the browser login flow (BFF).

    ---
    get:
      summary: Complete the browser login flow.
      description: >-
        Handles the issuer's redirect: validates the OAuth state, exchanges
        the authorization code for tokens, provisions/links the REANA user,
        synchronizes group memberships, stores the refresh token server-side
        and sets the authentication cookies.
      operationId: bff_oauth_callback
      parameters:
        - name: code
          in: query
          required: false
          type: string
        - name: state
          in: query
          required: false
          type: string
        - name: error
          in: query
          required: false
          type: string
      responses:
        302:
          description: Redirect back to the web application.
        403:
          description: OAuth state validation failed.
        502:
          description: Token exchange with the issuer failed.
    """
    if not _bff_active():
        return jsonify(message="Browser login is not enabled."), 404
    try:
        state_data = consume_state(request.args.get("state", ""))
    except InvalidOAuthState as error:
        return jsonify(message=str(error)), 403
    next_url = safe_next_url(state_data.get("next"))
    if "error" in request.args:
        logging.warning(
            "Issuer returned an authorization error: %s",
            request.args.get("error"),
        )
        response = redirect(f"{next_url}?login_error=authorization")
        return clear_state_cookie(response)
    try:
        token_response = requests.post(
            get_endpoint("token_url"),
            data={
                "grant_type": "authorization_code",
                "code": request.args.get("code", ""),
                "redirect_uri": _callback_redirect_uri(),
                "code_verifier": state_data.get("verifier", ""),
                "client_id": REANA_AUTH["web_client_id"],
                "client_secret": REANA_AUTH["web_client_secret"],
            },
            timeout=REANA_AUTH["http_timeout"],
        )
        token_response.raise_for_status()
        token_body = token_response.json()
        access_token = token_body["access_token"]
    except (requests.RequestException, ValueError, KeyError) as error:
        logging.error("Authorization code exchange failed: %s", error)
        return jsonify(message="Token exchange with the issuer failed."), 502
    try:
        claims = validate_access_token(access_token)
    except InvalidTokenError as error:
        logging.error("Issuer returned an invalid access token: %s", error)
        return jsonify(message="Token exchange with the issuer failed."), 502

    try:
        user = get_or_provision_user(claims, access_token)
        # Re-sync group memberships on every login, not only on first
        # sight (JIT syncs internally only when provisioning).
        try:
            userinfo = fetch_userinfo(access_token)
            sync_user_groups_from_userinfo(user, userinfo)
        except Exception:
            logging.exception("Group sync failed during login.")
    except MissingRoleError:
        # The session is still established: /api/you will answer 403 and
        # the UI shows the "access not granted" state.
        logging.info("User without the required role logged in via BFF.")
    except ProvisioningError as error:
        logging.warning("Could not provision user at login: %s", error)
        response = redirect(f"{next_url}?login_error=provisioning")
        return clear_state_cookie(response)

    sid = claims.get("sid") or claims["sub"]
    store_session(
        sid,
        token_body.get("refresh_token", ""),
        token_body.get("id_token", ""),
        access_token,
    )
    response = redirect(next_url)
    set_auth_cookies(response, access_token)
    return clear_state_cookie(response)


@blueprint.route("/logout", methods=["POST"])
def logout():
    r"""End the browser session (BFF).

    ---
    post:
      summary: End the browser session.
      description: >-
        Deletes the server-side session (refresh token), clears the
        authentication cookies and returns the issuer's RP-initiated
        logout URL for the web application to navigate to.
      operationId: bff_logout
      produces:
        - application/json
      responses:
        200:
          description: >-
            Session ended. The response contains the issuer logout URL.
          schema:
            type: object
            properties:
              logout_url:
                type: string
        401:
          description: No session cookie present.
        403:
          description: CSRF validation failed.
    """
    if not _bff_active():
        return jsonify(message="Browser login is not enabled."), 404
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        return jsonify(message="User not signed in."), 401
    if not csrf_ok():
        return jsonify(message="CSRF token missing or invalid."), 403
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
        # Even with an unusable cookie we still clear it locally.
        logging.info("Logout with unusable session token: %s", error)
    response = jsonify(logout_url=logout_url)
    return clear_auth_cookies(response)
