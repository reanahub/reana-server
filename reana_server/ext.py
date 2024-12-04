# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021, 2022, 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask extension REANA-Server."""

import logging

from flask import jsonify
from flask_limiter.errors import RateLimitExceeded
from marshmallow.exceptions import ValidationError
from reana_commons.config import REANA_LOG_FORMAT, REANA_LOG_LEVEL
from sqlalchemy_utils.types.encrypted.padding import InvalidPaddingError
from werkzeug.exceptions import UnprocessableEntity

from invenio_oauthclient.signals import account_info_received
from flask_security.signals import user_registered


from reana_server import config
from reana_server.utils import (
    _create_and_associate_local_user,
    _create_and_associate_oauth_user,
)


def handle_rate_limit_error(error: RateLimitExceeded):
    """Error handler for flask_limiter exception ``RateLimitExceeded``.

    This error handler is needed to display useful error messages, instead of the
    generic default one, when rate limit exception is raised.
    """
    rate_limit = error.description or str(error)
    error_message = (
        f'Too many requests in a given amount of time. Only "{rate_limit}" allowed.'
    )
    return jsonify({"message": error_message}), 429


def handle_args_validation_error(error: UnprocessableEntity):
    """Error handler for werkzeug exception ``UnprocessableEntity``.

    This error handler is needed to display useful error messages, instead of the
    generic default one, when marshmallow argument validation fails.
    """
    error_message = error.description or str(error)

    exception = getattr(error, "exc", None)
    if isinstance(exception, ValidationError):
        validation_messages = []
        for field, messages in exception.normalized_messages().items():
            validation_messages.append(
                "Field '{}': {}".format(field, ", ".join(messages))
            )
        error_message = ". ".join(validation_messages)

    return jsonify({"message": error_message}), 400


def handle_invalid_padding_error(error: InvalidPaddingError):
    """Error handler for sqlalchemy_utils exception ``InvalidPaddingError``.

    This error handler raises an exception with a more understandable message.
    """
    raise InvalidPaddingError(
        "Error decrypting the database. Did you set the correct secret key? "
        "If you changed the secret key, did you run the migration command?"
    ) from error


class REANA(object):
    """REANA Invenio app.

    This is used to initialise REANA as a Flask/Invenio extension,
    and this is used in production.

    See the docsting of `reana_server/factory.py` for more details.
    """

    def __init__(self, app=None):
        """Extension initialization."""
        logging.basicConfig(level=REANA_LOG_LEVEL, format=REANA_LOG_FORMAT, force=True)
        werkzeug_logger = logging.getLogger("werkzeug")
        werkzeug_logger.propagate = False
        if app:
            self.app = app
            self.init_app(app)

    def init_app(self, app):
        """Flask application initialization."""
        self.init_config(app)
        self.init_error_handlers(app)

        account_info_received.connect(_create_and_associate_oauth_user)
        user_registered.connect(_create_and_associate_local_user)

        @app.teardown_appcontext
        def shutdown_reana_db_session(response_or_exc):
            """Close session on app teardown."""
            from reana_db.database import Session as reana_db_session
            from invenio_db import db as invenio_db

            reana_db_session.remove()
            invenio_db.session.remove()
            return response_or_exc

    def init_config(self, app):
        """Initialize configuration."""
        for k in dir(config):
            if k.startswith("REANA_"):
                app.config.setdefault(k, getattr(config, k))

    def init_error_handlers(self, app):
        """Initialize custom error handlers."""
        app.register_error_handler(RateLimitExceeded, handle_rate_limit_error)
        app.register_error_handler(UnprocessableEntity, handle_args_validation_error)
        app.register_error_handler(InvalidPaddingError, handle_invalid_padding_error)
