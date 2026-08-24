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
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from flask import Blueprint, jsonify, redirect, request

from reana_server.auth import (
    MissingRoleError,
    ProvisioningError,
    get_or_provision_user,
    validate_access_token,
)
from reana_server.auth.tokens import validate_id_token
from reana_server.auth.config import (
    get_auth_config,
    get_issuer_request_kwargs,
    raise_for_issuer_status,
)
from reana_server.auth.discovery import get_endpoint, get_openid_configuration
from reana_server.auth.errors import (
    AuthError,
    InvalidTokenError,
    IssuerMisconfiguredError,
    IssuerUnavailableError,
    SessionUnavailableError,
)
from reana_server.auth.sessions import (
    AUTH_COOKIE,
    SESSION_COOKIE,
    clear_auth_cookies,
    csrf_ok,
    decode_expired_token,
    delete_session,
    get_session,
    session_matches_identity,
    set_auth_cookies,
    store_session,
)
from reana_server.config import REANA_URL
from reana_server.oauth_state import (
    InvalidOAuthState,
    clear_state_cookie,
    consume_state,
    issue_state,
    safe_next_url,
)

blueprint = Blueprint("auth", __name__)


def _bff_active():
    auth_config = get_auth_config()
    return bool(auth_config["bff_enabled"] and auth_config["issuer"])


def _callback_redirect_uri():
    return f"{REANA_URL}/api/oauth/callback"


def _login_error_redirect(next_url, error_code):
    """Build a redirect without dropping existing query params or fragments."""
    parts = urlparse(next_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "login_error"
    ]
    query.append(("login_error", error_code))
    return urlunparse(parts._replace(query=urlencode(query)))


def _client_facing_endpoint_url(endpoint_url):
    """Return endpoint URL rewritten to the public issuer host when possible.

    In-cluster deployments reach the issuer over an internal service URL,
    but host- and browser-side clients must be handed the issuer's public
    host. When the endpoint lives under the configured issuer's path it is
    rewritten onto the issuer's public scheme/host.
    """
    auth_config = get_auth_config()
    issuer = auth_config["issuer"]
    if not issuer or not endpoint_url:
        return endpoint_url
    issuer_parts = urlparse(issuer)
    endpoint_parts = urlparse(endpoint_url)
    if not issuer_parts.scheme or not issuer_parts.netloc:
        return endpoint_url
    if not endpoint_parts.scheme or not endpoint_parts.netloc:
        return endpoint_url
    issuer_path = issuer_parts.path.rstrip("/")
    endpoint_path = endpoint_parts.path
    backchannel = auth_config.get("backchannel_base_url", "")
    if backchannel:
        backchannel_parts = urlparse(backchannel)
        backchannel_path = backchannel_parts.path.rstrip("/")
        if (
            endpoint_parts.scheme.lower() == backchannel_parts.scheme.lower()
            and endpoint_parts.netloc.lower() == backchannel_parts.netloc.lower()
            and (
                endpoint_path == backchannel_path
                or endpoint_path.startswith(backchannel_path + "/")
            )
        ):
            endpoint_path = issuer_path + endpoint_path[len(backchannel_path) :]
    if issuer_path and not (
        endpoint_path == issuer_path or endpoint_path.startswith(issuer_path + "/")
    ):
        return endpoint_url
    return urlunparse(
        (
            issuer_parts.scheme,
            issuer_parts.netloc,
            endpoint_path,
            endpoint_parts.params,
            endpoint_parts.query,
            endpoint_parts.fragment,
        )
    )


def _client_facing_openid_configuration(document):
    """Return discovery document suitable for host/browser-side clients."""
    public_document = dict(document)
    issuer = get_auth_config()["issuer"]
    if issuer:
        public_document["issuer"] = issuer
    for field in (
        "authorization_endpoint",
        "token_endpoint",
        "userinfo_endpoint",
        "jwks_uri",
        "end_session_endpoint",
        "device_authorization_endpoint",
    ):
        if field in public_document:
            public_document[field] = _client_facing_endpoint_url(public_document[field])
    return public_document


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
      security: []
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
              reana_cli_client_id:
                type: string
        500:
          description: The identity provider integration is not correctly configured.
          schema:
            type: object
            properties:
              message:
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
        configuration = _client_facing_openid_configuration(get_openid_configuration())
    except IssuerMisconfiguredError as error:
        logging.error("Could not relay OpenID configuration: %s", error)
        return (
            jsonify(message="Could not fetch the issuer's OpenID configuration."),
            500,
        )
    except IssuerUnavailableError as error:
        logging.error("Could not relay OpenID configuration: %s", error)
        return (
            jsonify(message="Could not fetch the issuer's OpenID configuration."),
            502,
        )
    cli_client_id = get_auth_config()["cli_client_id"]
    configuration["reana_cli_client_id"] = cli_client_id
    configuration["reana_client_id"] = cli_client_id  # legacy alias
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
      security: []
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
        500:
          description: The identity provider integration is not correctly configured.
        502:
          description: The issuer's endpoints could not be resolved.
    """
    if not _bff_active():
        return jsonify(message="Browser login is not enabled."), 404
    auth_config = get_auth_config()
    next_url = safe_next_url(request.args.get("next"))
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    try:
        authorization_url = get_endpoint("authorization_url")
    except IssuerMisconfiguredError as error:
        logging.error("Could not resolve authorization endpoint: %s", error)
        return jsonify(message="Could not reach the identity provider."), 500
    except IssuerUnavailableError as error:
        logging.error("Could not resolve authorization endpoint: %s", error)
        return jsonify(message="Could not reach the identity provider."), 502
    nonce = secrets.token_urlsafe(32)
    response = redirect("placeholder")
    state = issue_state(response, verifier=verifier, next=next_url, nonce=nonce)
    response.headers["Location"] = (
        authorization_url
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": auth_config["web_client_id"],
                "redirect_uri": _callback_redirect_uri(),
                "scope": auth_config["scopes"],
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    return response


@blueprint.route("/oauth/callback", methods=["GET"])
def oauth_callback():  # noqa: C901
    r"""Complete the browser login flow (BFF).

    ---
    get:
      summary: Complete the browser login flow.
      description: >-
        Handles the issuer's redirect: validates the OAuth state, exchanges
        the authorization code for tokens, provisions/links the REANA user,
        stores the refresh token server-side and sets the authentication
        cookies.
      operationId: bff_oauth_callback
      security: []
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
        503:
          description: Browser session storage is temporarily unavailable.
        404:
          description: Browser login is not enabled.
        500:
          description: The identity provider integration is not correctly configured.
    """
    if not _bff_active():
        return jsonify(message="Browser login is not enabled."), 404
    auth_config = get_auth_config()
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
        response = redirect(_login_error_redirect(next_url, "authorization"))
        return clear_state_cookie(response)
    try:
        token_response = requests.post(
            get_endpoint("token_url"),
            data={
                "grant_type": "authorization_code",
                "code": request.args.get("code", ""),
                "redirect_uri": _callback_redirect_uri(),
                "code_verifier": state_data.get("verifier", ""),
                "client_id": auth_config["web_client_id"],
                "client_secret": auth_config["web_client_secret"],
            },
            **get_issuer_request_kwargs(),
        )
        raise_for_issuer_status(token_response)
        token_body = token_response.json()
        access_token = token_body["access_token"]
    except IssuerMisconfiguredError as error:
        logging.error("Authorization code exchange failed: %s", error)
        return jsonify(message="Token exchange with the issuer failed."), 500
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        IssuerUnavailableError,
    ) as error:
        logging.error("Authorization code exchange failed: %s", error)
        return jsonify(message="Token exchange with the issuer failed."), 502
    try:
        claims = validate_access_token(access_token)
        # Bind this authorization response to the browser login request.
        nonce = state_data.get("nonce")
        if not nonce:
            raise InvalidTokenError("OAuth state is missing the OIDC nonce.")
        id_token = token_body.get("id_token")
        id_claims = validate_id_token(id_token, nonce=nonce)
        if id_claims["sub"] != claims["sub"]:
            raise InvalidTokenError(
                "ID token subject does not match the access token subject."
            )
    except InvalidTokenError as error:
        logging.error("Issuer returned an invalid token: %s", error)
        return jsonify(message="Token exchange with the issuer failed."), 502
    except IssuerMisconfiguredError as error:
        logging.error("Identity provider misconfigured during validation: %s", error)
        return (
            jsonify(
                message=(
                    "The identity provider integration is not correctly "
                    "configured. Please contact the administrator."
                )
            ),
            500,
        )
    except AuthError as error:
        logging.error("Identity provider validation failed: %s", error)
        return jsonify(message="The identity provider is temporarily unavailable."), 502

    try:
        user, _is_new = get_or_provision_user(claims, access_token)
    except MissingRoleError:
        # The session is still established: /api/you will answer 403 and
        # the UI shows the "access not granted" state.
        logging.info("User without the required role logged in via BFF.")
    except IssuerUnavailableError as error:
        # A UserInfo outage during first-login provisioning is an availability
        # failure, not an authorization denial; surface it like the token
        # exchange path rather than letting it escape as an unhandled 500.
        logging.error("Identity provider unavailable during provisioning: %s", error)
        response = jsonify(message="The identity provider is temporarily unavailable.")
        return clear_state_cookie(response), 503
    except IssuerMisconfiguredError as error:
        # Distinct from the outage case above: retrying won't help, an
        # administrator must fix the issuer/discovery configuration. Without
        # this branch a misconfigured endpoint (e.g. a discovery document
        # that doesn't advertise userinfo_endpoint) would previously escape
        # this try block entirely as a genuinely unhandled 500.
        logging.error("Identity provider misconfigured during provisioning: %s", error)
        response = jsonify(
            message=(
                "The identity provider integration is not correctly "
                "configured. Please contact the administrator."
            )
        )
        return clear_state_cookie(response), 500
    except ProvisioningError as error:
        logging.warning("Could not provision user at login: %s", error)
        response = redirect(_login_error_redirect(next_url, "provisioning"))
        return clear_state_cookie(response)

    sid = secrets.token_urlsafe(32)
    try:
        store_session(
            sid,
            token_body.get("refresh_token", ""),
            id_token,
            access_token,
            issuer=claims["iss"],
            subject=claims["sub"],
            client_id=auth_config["web_client_id"],
            created_at=time.time(),
        )
    except SessionUnavailableError as error:
        logging.error("Could not establish browser session: %s", error)
        response = jsonify(message=str(error))
        return clear_state_cookie(response), 503
    response = redirect(next_url)
    set_auth_cookies(response, access_token, session_id=sid)
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
      security: []
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
        503:
          description: Browser session storage is temporarily unavailable.
        404:
          description: Browser login is not enabled.
    """
    if not _bff_active():
        return jsonify(message="Browser login is not enabled."), 404
    auth_config = get_auth_config()
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        return jsonify(message="User not signed in."), 401
    if not csrf_ok(request.headers, request.cookies):
        return jsonify(message="CSRF token missing or invalid."), 403
    logout_url = ""
    try:
        claims = decode_expired_token(token)
        sid = request.cookies.get(SESSION_COOKIE)
        if not sid:
            raise InvalidTokenError("Browser session id is missing.")
        session_data = get_session(sid)
        if session_data and not session_matches_identity(
            session_data, claims["iss"], claims["sub"]
        ):
            # Clear only this response's mismatched cookies. The referenced
            # Redis session may belong to another browser identity and must
            # not be destroyed or disclosed through its ID-token hint.
            raise InvalidTokenError(
                "Browser access and session cookies belong to different identities."
            )
        delete_session(sid)
        params = {
            "post_logout_redirect_uri": REANA_URL,
            "client_id": auth_config["web_client_id"],
        }
        if session_data and session_data.get("idt"):
            params["id_token_hint"] = session_data["idt"]
        logout_url = get_endpoint("end_session_url") + "?" + urlencode(params)
    except SessionUnavailableError as error:
        logging.warning("Could not remove browser session: %s", error)
        response = jsonify(message=str(error))
        return clear_auth_cookies(response), 503
    except (InvalidTokenError, AuthError) as error:
        # Even with an unusable cookie we still clear it locally.
        logging.info("Logout with unusable session token: %s", error)
    response = jsonify(logout_url=logout_url)
    return clear_auth_cookies(response)
