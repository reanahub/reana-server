# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020, 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server function decorators."""

import functools
import logging
import traceback

from flask import jsonify, request
from flask_login import current_user
from reana_commons.errors import REANAQuotaExceededError

from reana_server.utils import (
    _get_user_from_invenio_user,
    get_user_from_token,
    get_quota_excess_message,
)


def signin_required(include_gitlab_login=False, token_required=True):
    """Check if the user is signed in or the access token is valid and return the user."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                user = None
                if current_user.is_authenticated:
                    user = _get_user_from_invenio_user(current_user.email)
                elif include_gitlab_login and "X-Gitlab-Token" in request.headers:
                    user = get_user_from_token(request.headers["X-Gitlab-Token"])
                elif "access_token" in request.args:
                    user = get_user_from_token(request.args.get("access_token"))
                if not user:
                    return jsonify(message="User not signed in"), 401
                if token_required and not user.active_token:
                    return jsonify(message="User has no active tokens"), 401
            except ValueError as e:
                logging.error(traceback.format_exc())
                return jsonify({"message": str(e)}), 403

            return func(*args, **kwargs, user=user)

        return wrapper

    return decorator


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
