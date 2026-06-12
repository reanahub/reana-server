# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020, 2022, 2023 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server function decorators."""

import functools
import hmac
import logging
import traceback

from flask import after_this_request, jsonify, request
from reana_commons.errors import REANAQuotaExceededError
from reana_db.database import Session
from reana_db.models import User

from reana_server.auth import (
    AuthError,
    InvalidTokenError,
    MissingRoleError,
    ProvisioningError,
    get_or_provision_user,
    require_role,
    validate_access_token,
)
from reana_server.auth.sessions import (
    AUTH_COOKIE,
    csrf_ok,
    decode_expired_token,
    refresh_session,
    set_auth_cookies,
)
from reana_server.config import REANA_AUTH
from reana_server.utils import get_quota_excess_message


class _CSRFError(AuthError):
    """CSRF double-submit validation failed (HTTP 403)."""


def signin_required(include_gitlab_login=False, token_required=True):
    """Authenticate the request and inject the REANA ``user`` kwarg.

    Credential order: ``Authorization: Bearer`` JWT, then the BFF auth
    cookie (with CSRF double-submit on mutating methods and transparent
    refresh of expired access tokens), then — when ``include_gitlab_login``
    — the per-user GitLab webhook secret in ``X-Gitlab-Token``.

    ``token_required`` historically meant "user has an active REANA token";
    it now means "user has the required REANA role" (``reana:user`` by
    default). Endpoints with ``token_required=False`` (``/you``, ``/info``,
    ``/status``, workflow listing) stay accessible to authenticated users
    without the role so the UI can show an "access not granted" state.
    Note that a *first-time* identity without the role still gets 403
    everywhere: just-in-time provisioning refuses to create users that may
    not use REANA.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                user, refreshed_token = _authenticate(
                    include_gitlab_login, token_required
                )
            except InvalidTokenError as e:
                return jsonify(message=str(e)), 401
            except (MissingRoleError, _CSRFError) as e:
                return jsonify(message=str(e)), 403
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

                @after_this_request
                def _set_refreshed_cookie(response):
                    return set_auth_cookies(response, refreshed_token)

            return func(*args, **kwargs, user=user)

        return wrapper

    return decorator


def _authenticate(include_gitlab_login, token_required):
    """Resolve the request credentials to a REANA user.

    :return: tuple ``(user_or_none, refreshed_cookie_token_or_none)``.
    """
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        raw_token = authorization[len("Bearer ") :]
        claims = validate_access_token(raw_token)
        if token_required:
            require_role(claims)
        return get_or_provision_user(claims, raw_token), None

    cookie_token = request.cookies.get(AUTH_COOKIE)
    if cookie_token and REANA_AUTH["bff_enabled"]:
        if request.method not in ("GET", "HEAD", "OPTIONS") and not csrf_ok():
            raise _CSRFError("CSRF token missing or invalid.")
        raw_token = cookie_token
        refreshed = None
        try:
            claims = validate_access_token(raw_token)
        except InvalidTokenError:
            # Expired (or otherwise rejected) cookie token: attempt a
            # transparent refresh via the server-side session.
            claims = decode_expired_token(raw_token)
            refreshed = refresh_session(claims.get("sid") or claims["sub"])
            if not refreshed:
                raise InvalidTokenError(
                    "Session expired, please log in again."
                )
            raw_token = refreshed
            claims = validate_access_token(raw_token)
        if token_required:
            require_role(claims)
        return get_or_provision_user(claims, raw_token), refreshed

    if include_gitlab_login and "X-Gitlab-Token" in request.headers:
        return (
            _get_user_from_gitlab_secret(request.headers["X-Gitlab-Token"]),
            None,
        )

    return None, None


def _get_user_from_gitlab_secret(secret_value):
    """Authenticate a GitLab webhook via the per-user webhook secret."""
    user = (
        Session.query(User)
        .filter_by(gitlab_webhook_secret=secret_value)
        .one_or_none()
    )
    if not user or not hmac.compare_digest(
        user.gitlab_webhook_secret or "", secret_value
    ):
        raise InvalidTokenError("Invalid GitLab webhook token.")
    # No role gate: the per-user webhook secret is the credential
    # (AUTH_ARCHITECTURE.md §5.6).
    return user


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
