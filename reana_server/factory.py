# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021, 2022, 2024, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask-application factory for Reana-Server."""

import logging

from flask import Flask, current_app, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from marshmallow.exceptions import ValidationError
from reana_commons.config import REANA_LOG_FORMAT, REANA_LOG_LEVEL
from reana_db.database import Session
from sqlalchemy_utils.types.encrypted.padding import InvalidPaddingError
from werkzeug.exceptions import UnprocessableEntity
from werkzeug.middleware.proxy_fix import ProxyFix

from reana_server.auth.sessions import AUTH_COOKIE


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


def _useragent_and_ip_limit_key():
    """Create key for the rate limiting (clone of invenio-app's key func)."""
    return str(request.user_agent) + request.remote_addr


def _set_rate_limit():
    """Resolve the rate limit for the current request.

    Per-endpoint limits win; otherwise requests presenting credentials
    (Bearer header or the BFF auth cookie) get the authenticated limit,
    everything else the guest limit. (Clone of invenio-app's
    ``set_rate_limit``, with credential presence replacing flask-login.)
    """
    endpoint_limits = current_app.config.get("RATELIMIT_PER_ENDPOINT", {})
    if request.endpoint in endpoint_limits:
        return endpoint_limits[request.endpoint]
    has_credentials = request.headers.get("Authorization", "").startswith(
        "Bearer "
    ) or bool(request.cookies.get(AUTH_COOKIE))
    if has_credentials:
        return current_app.config["RATELIMIT_AUTHENTICATED_USER"]
    return current_app.config["RATELIMIT_GUEST_USER"]


def _validate_secret_key(app):
    """Refuse to start without a strong session secret."""
    if not app.config.get("SECRET_KEY"):
        raise ValueError(
            "SECRET_KEY is unset. Provide a strong random value via "
            "secrets.reana.REANA_SECRET_KEY in your Helm values, e.g. "
            "`--set secrets.reana.REANA_SECRET_KEY=$(openssl rand -hex 32)`. "
            "For existing clusters that need to rotate, see: "
            "https://blog.reana.io/posts/2024/reana-0.9.4/"
        )


def create_app(config_mapping=None):
    """REANA Server application factory.

    Creates the single Flask app used in production (uwsgi via
    ``reana_server.wsgi``), in debug mode (``flask run``), by the Flask CLI
    (``flask reana-admin ...``), in the tests and by
    ``generate_openapi_spec.py``. Authentication is stateless JWT
    validation against the configured OIDC issuer (see
    ``reana_server.auth``); there is no server-side login session.
    """
    logging.basicConfig(level=REANA_LOG_LEVEL, format=REANA_LOG_FORMAT, force=True)
    logging.getLogger("werkzeug").propagate = False

    app = Flask(__name__)
    app.config.from_object("reana_server.config")
    if config_mapping:
        app.config.from_mapping(config_mapping)
    _validate_secret_key(app)
    if not app.config["REANA_AUTH"]["issuer"]:
        logging.warning(
            "REANA_AUTH_ISSUER is not configured: every authenticated API "
            "request will be rejected until an OIDC issuer is set."
        )

    app.session = Session

    # Trust the X-Forwarded-* headers set by the ingress/reverse proxy so
    # that generated URLs use https and Secure cookies work.
    app.wsgi_app = ProxyFix(app.wsgi_app, **app.config.get("PROXYFIX_CONFIG", {}))

    # Rate limiting (application-wide dynamic limit + per-endpoint table).
    Limiter(
        app,
        key_func=_useragent_and_ip_limit_key,
        application_limits=[_set_rate_limit],
    )

    if app.config.get("REST_ENABLE_CORS"):
        CORS(app)

    @app.after_request
    def _secure_headers(response):
        """Minimal secure-header subset (TLS is terminated by the ingress)."""
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31556926; includeSubDomains"
            )
        return response

    # Register API routes
    from .rest import (
        auth,
        config,
        gitlab,
        groups,
        ping,
        secrets,
        status,
        users,
        workflows,
        info,
        launch,
        quota,
    )  # noqa

    app.register_blueprint(ping.blueprint, url_prefix="/api")
    app.register_blueprint(workflows.blueprint, url_prefix="/api")
    app.register_blueprint(users.blueprint, url_prefix="/api")
    app.register_blueprint(secrets.blueprint, url_prefix="/api")
    app.register_blueprint(gitlab.blueprint, url_prefix="/api")
    app.register_blueprint(config.blueprint, url_prefix="/api")
    app.register_blueprint(status.blueprint, url_prefix="/api")
    app.register_blueprint(info.blueprint, url_prefix="/api")
    app.register_blueprint(launch.blueprint, url_prefix="/api")
    app.register_blueprint(quota.blueprint, url_prefix="/api")
    app.register_blueprint(auth.blueprint, url_prefix="/api")
    app.register_blueprint(groups.blueprint, url_prefix="/api")

    app.register_error_handler(RateLimitExceeded, handle_rate_limit_error)
    app.register_error_handler(UnprocessableEntity, handle_args_validation_error)
    app.register_error_handler(InvalidPaddingError, handle_invalid_padding_error)

    @app.teardown_appcontext
    def shutdown_session(response_or_exc):
        """Close the reana-db session on app teardown."""
        current_app.session.remove()
        return response_or_exc

    return app
