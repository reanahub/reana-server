# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021, 2022, 2024, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask-application factory for Reana-Server."""

import logging

from flask import Flask, current_app, g, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_talisman import Talisman
from marshmallow.exceptions import ValidationError
from reana_commons.config import REANA_LOG_FORMAT, REANA_LOG_LEVEL
from reana_db.database import Session
from sqlalchemy_utils.types.encrypted.padding import InvalidPaddingError
from werkzeug.exceptions import UnprocessableEntity
from werkzeug.middleware.proxy_fix import ProxyFix

from reana_server.auth.sessions import AUTH_COOKIE
from reana_server.auth.discovery import validate_auth_configuration
from reana_server.auth.errors import AuthError
from reana_server.auth.tokens import validate_access_token
from reana_server.config import _positive_seconds_or_none
from reana_server.utils import initialise_workspace_umask

_SECURITY_HEADERS = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Permissions-Policy": (
        "accelerometer=(), ambient-light-sensor=(), camera=(), "
        "display-capture=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
}


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


ARGUMENT_LOCATIONS = frozenset(
    {
        "cookies",
        "files",
        "form",
        "headers",
        "json",
        "json_or_form",
        "match_info",
        "path",
        "query",
        "querystring",
        "view_args",
    }
)
"""Request locations webargs namespaces its validation messages under."""


def _flatten_validation_messages(messages, path=()):
    """Yield ``(field path, messages)`` leaves of a marshmallow error tree."""
    if isinstance(messages, dict):
        for key, value in messages.items():
            yield from _flatten_validation_messages(value, path + (str(key),))
    elif isinstance(messages, (list, tuple)):
        yield path, [str(message) for message in messages]
    else:
        yield path, [str(messages)]


def handle_args_validation_error(error: UnprocessableEntity):
    """Error handler for werkzeug exception ``UnprocessableEntity``.

    This error handler is needed to display useful error messages, instead of the
    generic default one, when marshmallow argument validation fails.

    ``normalized_messages()`` nests the field errors under the request location
    webargs parsed (and once more per nested schema or collection), so the tree
    is flattened to leaves. Joining its top-level values directly would render
    the nested dictionary's *keys* and drop every actual complaint.
    """
    error_message = error.description or str(error)

    exception = getattr(error, "exc", None)
    if isinstance(exception, ValidationError):
        validation_messages = []
        for path, messages in _flatten_validation_messages(
            exception.normalized_messages()
        ):
            if len(path) > 1 and path[0] in ARGUMENT_LOCATIONS:
                path = path[1:]
            validation_messages.append(
                "Field '{}': {}".format(".".join(path), ", ".join(messages))
            )
        if validation_messages:
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


def _validated_rate_limit_claims():
    """Return the request's validated JWT claims for rate limiting, or ``None``.

    Only a signature-valid, unexpired JWT backed by the already-cached issuer
    keys qualifies; classification never performs discovery/JWKS network I/O, so
    a cold cache yields ``None`` (the guest bucket). The result is cached on
    ``g`` so the limit value and the limit key agree regardless of evaluation
    order and the token is validated at most once per request.
    """
    if "reana_rate_limit_claims" in g:
        return g.reana_rate_limit_claims
    authorization = request.headers.get("Authorization", "")
    token = None
    authorization_parts = authorization.split(None, 1)
    if (
        len(authorization_parts) == 2
        and authorization_parts[0].lower() == "bearer"
        and authorization_parts[1]
    ):
        token = authorization_parts[1]
    elif current_app.config.get("REANA_AUTH", {}).get("bff_enabled"):
        token = request.cookies.get(AUTH_COOKIE)
    claims = None
    if token:
        try:
            claims = validate_access_token(token, allow_remote=False)
        except AuthError:
            claims = None
        else:
            # Share the validated claims with the request auth path so the
            # token is not validated a second time this request.
            g.reana_validated_token = token
            g.reana_token_claims = claims
    g.reana_rate_limit_claims = claims
    return claims


def _rate_limit_key():
    """Key rate limiting by validated identity, else by client address.

    Authenticated requests are bucketed per ``(issuer, subject)`` so users
    sharing a NAT do not share one authenticated counter; unauthenticated
    requests fall back to the proxy-normalised client address.

    Accepted tradeoff: this replaces the previous IP-only bucket with no
    residual per-IP ceiling behind it, so an issuer that allows open
    self-service registration lets one source mint unlimited distinct
    identities, each with its own full RATELIMIT_AUTHENTICATED_USER budget
    -- unbounded in aggregate from that source. This is deliberately not
    addressed at the application layer: Flask-Limiter's ``Limiter`` here
    has exactly one ``key_func`` for the whole instance, so a genuinely
    independent second per-IP dimension would need real new
    infrastructure (a second limiter sharing this app's storage backend),
    not a small addition. For a deployment using a self-service issuer,
    bound aggregate abuse at the ingress/proxy layer (a coarse per-IP
    rate limit in front of REANA) or at the issuer (registration
    throttling), not here. REANA's primary institutional-SSO deployments
    (e.g. CERN Keycloak) don't allow open self-registration, so this
    doesn't apply to them.
    """
    claims = _validated_rate_limit_claims()
    if claims is not None:
        issuer = claims.get("iss", "")
        subject = claims.get("sub", "")
        if issuer and subject:
            return f"user:{issuer}:{subject}"
    return f"ip:{request.remote_addr or 'unknown'}"


def _set_rate_limit():
    """Resolve the rate limit for the current request.

    Per-endpoint limits win; otherwise only a signature-valid, unexpired JWT
    backed by the already-cached issuer keys gets the authenticated limit.
    Rate classification must never fetch discovery metadata or JWKS: a cold
    cache therefore uses the guest bucket for the first authenticated request,
    and endpoint authentication warms the cache afterwards.
    """
    if (
        request.endpoint == "workflows.set_workflow_status"
        and request.args.get("status") == "start"
    ):
        return current_app.config["REANA_RATELIMIT_SLOW"]
    endpoint_limits = current_app.config.get("RATELIMIT_PER_ENDPOINT", {})
    if request.endpoint in endpoint_limits:
        return endpoint_limits[request.endpoint]
    if _validated_rate_limit_claims() is not None:
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


def _validate_gitlab_webhook_secret_lifetime(app):
    """Refuse to start with an unusable GitLab webhook secret lifetime."""
    configured_value = app.config.get("REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME")
    lifetime = _positive_seconds_or_none(configured_value)
    if lifetime is not None:
        app.config["REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME"] = lifetime
        return
    raise ValueError(
        "REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME must be a positive whole "
        "number of seconds, but is "
        f"{configured_value!r}. "
        "It bounds how long a delegated GitLab webhook secret stays authorized "
        "before the user must renew it from the REANA web interface."
    )


def create_app(config_mapping=None):
    """REANA Server application factory.

    Creates the single Flask app used in production (uwsgi via
    ``reana_server.wsgi``), in debug mode (``flask run``), by the Flask CLI
    (``flask reana-admin ...``), in the tests and by
    ``generate_openapi_spec.py``. Authentication is stateless JWT
    validation against the configured OIDC issuer (see
    ``reana_server.auth``). Browser refresh tokens are held in Redis-backed
    BFF sessions; API bearer tokens remain stateless.
    """
    initialise_workspace_umask()
    logging.basicConfig(level=REANA_LOG_LEVEL, format=REANA_LOG_FORMAT, force=True)
    logging.getLogger("werkzeug").propagate = False

    app = Flask(__name__)
    app.config.from_object("reana_server.config")
    # ``from_object`` retains mutable values by reference.  Give every Flask
    # application an independent auth mapping and merge factory overrides into
    # the complete default configuration.
    auth_config = dict(app.config["REANA_AUTH"])
    if config_mapping:
        app.config.from_mapping(config_mapping)
        auth_config.update(config_mapping.get("REANA_AUTH", {}))
    app.config["REANA_AUTH"] = auth_config
    with app.app_context():
        validate_auth_configuration()
    _validate_secret_key(app)
    _validate_gitlab_webhook_secret_lifetime(app)
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
        key_func=_rate_limit_key,
        application_limits=[_set_rate_limit],
    )

    if app.config.get("REST_ENABLE_CORS"):
        CORS(app)

    @app.after_request
    def _secure_headers(response):
        """Apply security headers equivalent to the pre-removal defaults."""
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value
        return response

    # Register after the REANA hook so Flask runs the REANA-specific header
    # additions after Talisman's defaults.
    Talisman(app, **app.config["APP_DEFAULT_SECURE_HEADERS"])

    # Register API routes
    from .rest import (
        auth,
        config,
        gitlab,
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

    app.register_error_handler(RateLimitExceeded, handle_rate_limit_error)
    app.register_error_handler(UnprocessableEntity, handle_args_validation_error)
    app.register_error_handler(InvalidPaddingError, handle_invalid_padding_error)

    @app.teardown_appcontext
    def shutdown_session(response_or_exc):
        """Close the reana-db session on app teardown."""
        current_app.session.remove()
        return response_or_exc

    return app
