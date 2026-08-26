# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020, 2022, 2023, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server function decorators."""

import functools
import hmac
import logging
import traceback

from flask import after_this_request, g, jsonify, request
from reana_commons.errors import REANAQuotaExceededError
from reana_db.database import Session
from reana_db.models import User
from reana_db.secrets import compute_lookup_digest

from reana_server.auth import (
    AuthError,
    InvalidTokenError,
    IssuerMisconfiguredError,
    IssuerUnavailableError,
    MissingRoleError,
    ProvisioningError,
    get_or_provision_user,
    require_role,
    validate_access_token,
)
from reana_server.auth.sessions import (
    AUTH_COOKIE,
    RefreshOutcome,
    SESSION_COOKIE,
    clear_auth_cookies,
    csrf_ok,
    decode_expired_token,
    refresh_session,
    set_auth_cookies,
)
from reana_server.auth.config import get_auth_config
from reana_server.auth.errors import (
    SessionUnavailableError,
    UnknownKeyHealthyBackoffError,
)
from reana_server.utils import get_quota_excess_message, naive_utcnow


class _CSRFError(AuthError):
    """CSRF double-submit validation failed (HTTP 403)."""


class _TerminalSessionError(InvalidTokenError):
    """Browser session is definitively invalid and its cookies must clear."""


def signin_required(include_gitlab_login=False, access_denied_code=None):
    """Authenticate the request and inject the REANA ``user`` kwarg.

    Credential order: ``Authorization: Bearer`` JWT, then the BFF auth
    cookie (with CSRF double-submit on mutating methods and transparent
    refresh of expired access tokens), then — when ``include_gitlab_login``
    — the per-user GitLab webhook secret in ``X-Gitlab-Token``.

    Every externally authenticated API endpoint enforces the configured
    coarse REANA access role. ``access_denied_code`` optionally adds a stable
    machine-readable code to role-denied responses (used by ``/api/you`` so
    the UI can distinguish missing entitlement from an operational error).
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                user, refreshed_token = _authenticate(include_gitlab_login)
            except _TerminalSessionError as e:
                return (
                    clear_auth_cookies(
                        jsonify(message=str(e), code="session_terminated")
                    ),
                    401,
                )
            except InvalidTokenError as e:
                logging.info("Access token validation failed: %s", e)
                return jsonify(message="Invalid access token."), 401
            except SessionUnavailableError as e:
                return jsonify(message=str(e)), 503
            except IssuerUnavailableError as e:
                # A single-line warning, not a full traceback at error level:
                # this is an expected external-dependency degradation, not a
                # bug, and an outage drives one of these per request -- a
                # multi-line stack trace per request is exactly the log
                # flooding that makes the real incident harder to read from
                # the log stream, not easier.
                logging.warning("Identity provider unavailable: %s", e)
                return (
                    jsonify(
                        message="The identity provider is temporarily unavailable."
                    ),
                    503,
                )
            except MissingRoleError as e:
                payload = {"message": str(e)}
                if access_denied_code:
                    payload["code"] = access_denied_code
                response = jsonify(payload)
                refreshed_token = getattr(e, "refreshed_token", None)
                if refreshed_token:
                    # The role was revoked right after a transparent refresh.
                    # Install the already-stored token so the browser stops
                    # presenting the expired one and re-refreshing on every
                    # request; the next request then fails this role check
                    # locally without another issuer round trip.
                    set_auth_cookies(
                        response,
                        refreshed_token,
                        existing_cookies=dict(request.cookies),
                    )
                return response, 403
            except _CSRFError as e:
                return jsonify(message=str(e)), 403
            except IssuerMisconfiguredError as e:
                # Distinct from IssuerUnavailableError: retrying won't help,
                # an administrator must fix the issuer/discovery configuration.
                # Must be caught ahead of the generic AuthError branch below,
                # which IssuerMisconfiguredError also is-a.
                logging.error("Identity provider misconfigured: %s", e)
                return (
                    jsonify(
                        message=(
                            "The identity provider integration is not correctly "
                            "configured. Please contact the administrator."
                        )
                    ),
                    500,
                )
            except (ProvisioningError, AuthError) as e:
                logging.error(traceback.format_exc())
                return jsonify(message=str(e)), 403
            except ValueError as e:
                logging.error(traceback.format_exc())
                return jsonify({"message": str(e)}), 403
            if not user:
                return (
                    jsonify(
                        message=(
                            "User not signed in. Please authenticate with "
                            "a Bearer token (see `reana-client login`) or "
                            "via the web login."
                        )
                    ),
                    401,
                )
            if refreshed_token:
                existing_cookies = dict(request.cookies)

                @after_this_request
                def _set_refreshed_cookie(response):
                    return set_auth_cookies(
                        response,
                        refreshed_token,
                        existing_cookies=existing_cookies,
                    )

            return func(*args, **kwargs, user=user)

        return wrapper

    return decorator


def _authorize_and_provision(claims, raw_token):
    """Enforce the role gate and resolve the REANA user for valid claims.

    Just-in-time provisioning may fetch UserInfo for identity/profile data,
    but authorization is based exclusively on the validated access token.
    """
    require_role(claims)
    user, _is_new = get_or_provision_user(claims, raw_token)
    return user


def _authenticate(include_gitlab_login):
    """Resolve the request credentials to a REANA user.

    :return: tuple ``(user_or_none, refreshed_cookie_token_or_none)``.
    """
    authorization = request.headers.get("Authorization", "")
    authorization_parts = authorization.split(None, 1)
    if (
        len(authorization_parts) == 2
        and authorization_parts[0].lower() == "bearer"
        and authorization_parts[1]
    ):
        raw_token = authorization_parts[1]
        try:
            claims = _validate_token_once(raw_token)
        except UnknownKeyHealthyBackoffError as error:
            # Only the bearer path makes this distinction: a one-shot API
            # call gains nothing from a 503 that just means "ask again in a
            # few seconds" for what is, in the common case, simply a forged
            # or stale credential. The cookie/BFF path deliberately does not
            # catch this here -- it falls through to the generic
            # IssuerUnavailableError handling below (503), since a genuine
            # key rotation arriving in this same window is more
            # consequential to misclassify for a live user session.
            raise InvalidTokenError(str(error)) from error
        return _authorize_and_provision(claims, raw_token), None

    cookie_token = request.cookies.get(AUTH_COOKIE)
    if cookie_token and get_auth_config()["bff_enabled"]:
        if request.method not in ("GET", "HEAD", "OPTIONS") and not csrf_ok(
            request.headers, request.cookies
        ):
            raise _CSRFError("CSRF token missing or invalid.")
        raw_token = cookie_token
        refreshed = None
        try:
            claims = _validate_token_once(raw_token)
        except InvalidTokenError:
            # Expired (or otherwise rejected) cookie token: attempt a
            # transparent refresh via the server-side session.
            try:
                claims = decode_expired_token(raw_token)
            except InvalidTokenError as error:
                # Bad signature, issuer, audience, or missing 'sub': the
                # cookie is not merely expired but definitively invalid, and
                # no refresh can fix that. Same terminal outcome as the
                # no-session-cookie and RefreshOutcome.TERMINAL cases below:
                # must raise _TerminalSessionError (not the generic
                # InvalidTokenError) so the wrapper clears cookies instead of
                # leaving the browser presenting the same unusable cookie on
                # every subsequent request -- e.g. after a key rotation or an
                # issuer/audience reconfiguration invalidates old cookies.
                raise _TerminalSessionError(str(error)) from error
            session_id = request.cookies.get(SESSION_COOKIE)
            if not session_id:
                # Same terminal outcome as RefreshOutcome.TERMINAL below: no
                # session to refresh with, so this is definitively over, not
                # a transient/ambiguous validation failure. Must raise
                # _TerminalSessionError (not the generic InvalidTokenError)
                # so the wrapper clears cookies AND preserves this message --
                # the generic except InvalidTokenError branch discards
                # str(e) and returns a fixed "Invalid access token." that
                # the reana-ui client can't distinguish from any other 401,
                # leaving the user stuck signed-in-but-broken with no
                # sign-out trigger.
                raise _TerminalSessionError("Session expired, please log in again.")
            refresh_result = refresh_session(
                session_id,
                previous_access_token=raw_token,
                expected_issuer=claims["iss"],
                expected_subject=claims["sub"],
            )
            if refresh_result.outcome is RefreshOutcome.TERMINAL:
                raise _TerminalSessionError("Session expired, please log in again.")
            if refresh_result.outcome is RefreshOutcome.TRANSIENT:
                raise SessionUnavailableError(
                    "Authentication session is temporarily unavailable."
                )
            refreshed = refresh_result.access_token
            raw_token = refreshed
            claims = validate_access_token(raw_token)
        try:
            user = _authorize_and_provision(claims, raw_token)
        except MissingRoleError as error:
            # Role revoked after a transparent refresh: carry the freshly stored
            # token out so the wrapper installs it on the 403. The next request
            # then validates it locally and fails the role check without
            # refreshing again.
            if refreshed is not None:
                error.refreshed_token = refreshed
            raise
        return user, refreshed

    if include_gitlab_login and "X-Gitlab-Token" in request.headers:
        return (
            _get_user_from_gitlab_secret(request.headers["X-Gitlab-Token"]),
            None,
        )

    return None, None


def _validate_token_once(raw_token):
    """Validate a JWT once per request and reuse rate-limit validation."""
    if getattr(g, "reana_validated_token", None) == raw_token:
        return g.reana_token_claims
    claims = validate_access_token(raw_token)
    g.reana_validated_token = raw_token
    g.reana_token_claims = claims
    return claims


def _get_user_from_gitlab_secret(secret_value):
    """Authenticate the dedicated per-user GitLab webhook secret."""
    user = (
        Session.query(User)
        .filter_by(gitlab_webhook_secret_digest=compute_lookup_digest(secret_value))
        .one_or_none()
    )
    if (
        user
        and hmac.compare_digest(user.gitlab_webhook_secret or "", secret_value)
        and user.gitlab_webhook_secret_expires_at
        and user.gitlab_webhook_secret_expires_at > naive_utcnow()
    ):
        return user
    raise InvalidTokenError("Invalid or expired GitLab webhook token.")


def check_quota(func):
    """Check user quota usage and prevent the function from running if exceeded."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            user = kwargs["user"]
            if user.has_exceeded_quota():
                message = get_quota_excess_message(user)
                raise REANAQuotaExceededError(message)
        except REANAQuotaExceededError as e:
            return jsonify({"message": e.message}), 403
        except Exception as e:
            logging.error(traceback.format_exc())
            return jsonify({"message": str(e)}), 500

        return func(*args, **kwargs)

    return wrapper
