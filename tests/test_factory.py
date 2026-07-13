# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2020, 2021, 2024, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for the REANA Flask application factory."""

from unittest.mock import Mock, patch

import pytest
from flask import Flask
from marshmallow.exceptions import ValidationError
from werkzeug.exceptions import UnprocessableEntity

from reana_server.auth import discovery, sessions, tokens
from reana_server.auth.sessions import _refresh_lock_ttl
from reana_server.auth.config import get_auth_config
from reana_server.auth.provision import email_linking_allowed
from reana_server.auth.errors import AuthError, InvalidTokenError
from reana_server.factory import (
    _rate_limit_key,
    _set_rate_limit,
    create_app,
    handle_args_validation_error,
)
from reana_server.rest.auth import _bff_active
from reana_server.status import UsersStatus

_TEST_ORIGIN = "https://example.com:30443"
_GUEST_LIMIT = "1 per second"
_AUTHENTICATED_LIMIT = "2 per second"

_EXPECTED_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Permissions-Policy": (
        "accelerometer=(), ambient-light-sensor=(), camera=(), "
        "display-capture=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
}


def _make_app(config=None):
    """Create a minimal test app using the production factory."""
    app_config = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "REST_ENABLE_CORS": True,
        "CORS_ORIGINS": [_TEST_ORIGIN],
        "CORS_SEND_WILDCARD": False,
        "CORS_SUPPORTS_CREDENTIALS": False,
        "REANA_AUTH": {"issuer": "", "bff_enabled": True},
        "RATELIMIT_ENABLED": False,
        "RATELIMIT_GUEST_USER": _GUEST_LIMIT,
        "RATELIMIT_AUTHENTICATED_USER": _AUTHENTICATED_LIMIT,
    }
    app_config.update(config or {})
    app = create_app(app_config)

    @app.route("/test")
    def test_route():
        return "OK"

    return app


def _render_args_validation_error(app, messages):
    """Return the message an argument-validation failure would produce."""
    error = UnprocessableEntity()
    error.exc = ValidationError(messages)
    with app.test_request_context():
        response, status_code = handle_args_validation_error(error)
    return response.get_json()["message"], status_code


def test_nested_argument_errors_render_the_actual_complaint():
    """Webargs namespaces messages per location; the leaf message must survive."""
    message, status_code = _render_args_validation_error(
        Flask(__name__), {"json": {"reana_specification": ["Unknown field."]}}
    )

    assert status_code == 400
    # Joining the nested dict directly used to render its keys instead, i.e.
    # "Field 'json': reana_specification".
    assert message == "Field 'reana_specification': Unknown field."


def test_deeply_nested_argument_errors_keep_their_field_path():
    """Nested schemas and collections keep an unambiguous field path."""
    message, _status_code = _render_args_validation_error(
        Flask(__name__),
        {"json": {"input_parameters": {"nested": ["Not a valid integer."]}}},
    )

    assert message == "Field 'input_parameters.nested': Not a valid integer."


def test_multiple_argument_errors_are_all_reported():
    """Every failing field is reported, not just the first one."""
    message, _status_code = _render_args_validation_error(
        Flask(__name__),
        {
            "query": {
                "status": ["Missing data for required field."],
                "size": ["Not a valid integer."],
            }
        },
    )

    assert "Field 'status': Missing data for required field." in message
    assert "Field 'size': Not a valid integer." in message


def test_security_headers_on_normal_response():
    """Security headers are set on every response."""
    with _make_app().test_client() as client:
        res = client.get("/test")

    for header, value in _EXPECTED_SECURITY_HEADERS.items():
        assert res.headers.get(header) == value


def test_factory_auth_override_reaches_all_auth_subsystems():
    """Factory auth overrides configure every runtime consumer and cache."""
    issuer = "https://override.example.org/realms/reana"
    redis_client = Mock()
    redis_client.scan_iter.return_value = ["reana:bff:session:one"]
    app = _make_app(
        {
            "REANA_AUTH": {
                "issuer": issuer,
                "bff_enabled": True,
                "jwks_ttl": 17,
                "redis_url": "redis://override.example.org/1",
                "redis_socket_connect_timeout": 1.25,
                "redis_socket_timeout": 2.5,
                "redis_health_check_interval": 9,
                "email_linking_enabled": True,
            }
        }
    )

    with app.app_context(), patch.object(
        sessions.redis.Redis,
        "from_url",
        return_value=redis_client,
    ) as from_url:
        assert get_auth_config()["issuer"] == issuer
        assert discovery.get_openid_configuration_url().startswith(issuer)
        assert tokens._claims_options()["iss"]["value"] == issuer
        assert tokens._get_jwks_cache().ttl == 17
        assert sessions.get_redis() is redis_client
        from_url.assert_called_once_with(
            "redis://override.example.org/1",
            decode_responses=True,
            socket_connect_timeout=1.25,
            socket_timeout=2.5,
            health_check_interval=9,
        )
        assert _bff_active()
        assert email_linking_allowed(
            issuer,
            "user@example.org",
            {"email_verified": True},
        )
        assert UsersStatus().active_web_users() == 1

    with patch("reana_server.rest.config.REANAConfig.load", return_value={}):
        response = app.test_client().get("/api/config")
    assert response.json["auth"]["bff_enabled"] is True


@pytest.mark.parametrize(
    ("auth_config", "message"),
    [
        (
            {"issuer": "http://idp.example.org/realms/reana"},
            "issuer must use HTTPS",
        ),
        (
            {
                "issuer": "https://idp.example.org/realms/reana",
                "backchannel_allow_http": True,
            },
            "cannot be enabled without",
        ),
        (
            {
                "issuer": "https://idp.example.org/realms/reana",
                "backchannel_base_url": (
                    "http://reana-keycloak:8080/keycloak/realms/reana"
                ),
            },
            "must use HTTPS",
        ),
        (
            {
                "issuer": "https://idp.example.org/realms/reana",
                "backchannel_base_url": "https://idp.internal/realms/reana",
                "backchannel_allow_http": True,
            },
            "base uses HTTPS",
        ),
        (
            {
                "issuer": "https://idp.example.org/realms/reana",
                "backchannel_base_url": "http://attacker.example/realms/reana",
                "backchannel_allow_http": True,
            },
            "must use HTTPS",
        ),
    ],
)
def test_factory_rejects_inconsistent_backchannel_policy(auth_config, message):
    """Invalid transport policy fails at process startup, not first login."""
    with pytest.raises(AuthError, match=message):
        _make_app({"REANA_AUTH": auth_config})


def test_factory_accepts_explicit_local_backchannel_policy():
    """The current bundled-Keycloak development configuration starts."""
    app = _make_app(
        {
            "REANA_AUTH": {
                "issuer": "https://localhost:30443/keycloak/realms/reana",
                "backchannel_base_url": (
                    "http://reana-keycloak:8080/keycloak/realms/reana"
                ),
                "backchannel_allow_http": True,
                "openid_config_url": (
                    "http://reana-keycloak:8080/keycloak/realms/reana/"
                    ".well-known/openid-configuration"
                ),
            }
        }
    )
    assert app.config["REANA_AUTH"]["backchannel_allow_http"] is True


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("audience", "access-token audience"),
        ("roles_claim", "roles claim"),
        ("required_role", "required role"),
        ("cli_client_id", "CLI client id"),
        ("web_client_id", "web client id"),
    ],
)
def test_factory_rejects_empty_authentication_contract_values(key, message):
    """Authentication cannot start with token validation gates disabled."""
    auth = {"issuer": "https://idp.example.org/realms/reana", key: ""}
    with pytest.raises(AuthError, match=message):
        _make_app({"REANA_AUTH": auth})


@pytest.mark.parametrize(
    "setting", ["0", "-1", "not-a-number", "", True, 1.5, None, "1e100"]
)
def test_factory_rejects_unusable_gitlab_webhook_lifetime(setting):
    """An unusable webhook lifetime is one startup error, not an import crash."""
    import reana_server.config as server_config

    assert server_config._positive_seconds_or_none(setting) is None
    with pytest.raises(ValueError, match="positive whole number of seconds"):
        _make_app(
            {
                "REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME": setting,
            }
        )


def test_get_rate_limit_warns_only_on_an_explicit_invalid_override(monkeypatch, caplog):
    """Unset is silent (the common case); an explicit bad value warns."""
    import logging as logging_module

    import reana_server.config as server_config

    monkeypatch.delenv("REANA_RATELIMIT_GUEST_USER", raising=False)
    with caplog.at_level(logging_module.WARNING):
        result = server_config._get_rate_limit(
            "REANA_RATELIMIT_GUEST_USER", "20 per second"
        )
    assert result == "20 per second"
    assert caplog.text == ""

    monkeypatch.setenv("REANA_RATELIMIT_GUEST_USER", "not-a-valid-limit")
    caplog.clear()
    with caplog.at_level(logging_module.WARNING):
        result = server_config._get_rate_limit(
            "REANA_RATELIMIT_GUEST_USER", "20 per second"
        )
    assert result == "20 per second"
    assert "REANA_RATELIMIT_GUEST_USER" in caplog.text
    assert "not-a-valid-limit" in caplog.text


def test_factory_normalizes_gitlab_webhook_lifetime_override():
    """Runtime consumers receive the validated factory override as an integer."""
    app = _make_app({"REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME": "17"})

    assert app.config["REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME"] == 17


def test_factory_accepts_helm_scientific_notation_lifetime():
    """Helm renders large unquoted YAML numbers in scientific notation.

    ``2592000`` becomes ``"2.592e+06"``; startup must accept that whole-second
    value instead of crashing, and normalise it to an integer.
    """
    app = _make_app({"REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME": "2.592e+06"})

    assert app.config["REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME"] == 2592000


def test_refresh_wait_timeout_tracks_the_issuer_timeout():
    """The concurrent-refresh wait is derived from, and below, the lock TTL."""
    import reana_server.config as server_config

    assert server_config._AUTH_REFRESH_WAIT_TIMEOUT == min(
        15.0, max(5.0, float(server_config._AUTH_HTTP_TIMEOUT))
    )
    app = _make_app()
    auth_config = app.config["REANA_AUTH"]
    assert auth_config["refresh_wait_timeout"] <= _refresh_lock_ttl(auth_config)


def test_auth_runtime_state_is_isolated_between_apps():
    """Discovery, JWKS, and Redis state belongs to one Flask application."""
    first = _make_app(
        {
            "REANA_AUTH": {
                "issuer": "https://first.example.org",
                "jwks_ttl": 11,
                "redis_url": "redis://first.example.org/1",
            }
        }
    )
    second = _make_app(
        {
            "REANA_AUTH": {
                "issuer": "https://second.example.org",
                "jwks_ttl": 22,
                "redis_url": "redis://second.example.org/1",
            }
        }
    )

    with first.app_context(), patch.object(
        sessions.redis.Redis, "from_url", return_value=Mock()
    ):
        first_discovery = discovery._get_discovery_state()
        first_jwks = tokens._get_jwks_cache()
        first_redis = sessions.get_redis()
    with second.app_context(), patch.object(
        sessions.redis.Redis, "from_url", return_value=Mock()
    ):
        second_discovery = discovery._get_discovery_state()
        second_jwks = tokens._get_jwks_cache()
        second_redis = sessions.get_redis()

    assert first.config["REANA_AUTH"]["issuer"].startswith("https://first")
    assert second.config["REANA_AUTH"]["issuer"].startswith("https://second")
    assert first_discovery is not second_discovery
    assert first_jwks is not second_jwks
    assert first_redis is not second_redis


def test_hsts_header_on_secure_response():
    """HSTS is set when Flask sees a secure request."""
    with _make_app().test_client() as client:
        res = client.get("/test", base_url="https://localhost")

    assert (
        res.headers.get("Strict-Transport-Security")
        == "max-age=31536000; includeSubDomains"
    )


def test_security_header_configuration_is_preserved():
    """Deployment overrides are still passed through to Flask-Talisman."""
    app = _make_app(
        {
            "APP_DEFAULT_SECURE_HEADERS": {
                "force_https": False,
                "frame_options": "SAMEORIGIN",
            }
        }
    )
    with app.test_client() as client:
        res = client.get("/test")

    assert res.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_cors_matching_origin_echoed_back():
    """A request from the allowed origin gets Access-Control-Allow-Origin echoed."""
    with _make_app().test_client() as client:
        res = client.get("/test", headers={"Origin": _TEST_ORIGIN})

    assert res.headers.get("Access-Control-Allow-Origin") == _TEST_ORIGIN


def test_cors_non_matching_origin_rejected():
    """A request from a different origin does not get Access-Control-Allow-Origin."""
    with _make_app().test_client() as client:
        res = client.get("/test", headers={"Origin": "https://example.com"})

    assert "Access-Control-Allow-Origin" not in res.headers


def test_valid_bearer_uses_authenticated_rate_limit():
    """A locally validated JWT receives the authenticated bucket."""
    app = _make_app()
    with app.test_request_context(
        headers={"Authorization": "Bearer signed-jwt"}
    ), patch(
        "reana_server.factory.validate_access_token",
        return_value={"sub": "user"},
    ) as validate:
        assert _set_rate_limit() == _AUTHENTICATED_LIMIT
    validate.assert_called_once_with("signed-jwt", allow_remote=False)


def test_lowercase_bearer_uses_authenticated_rate_limit():
    """Rate classification follows case-insensitive HTTP auth semantics."""
    app = _make_app()
    with app.test_request_context(
        headers={"Authorization": "bearer signed-jwt"}
    ), patch(
        "reana_server.factory.validate_access_token",
        return_value={"sub": "user"},
    ) as validate:
        assert _set_rate_limit() == _AUTHENTICATED_LIMIT
    validate.assert_called_once_with("signed-jwt", allow_remote=False)


def test_fake_bearer_uses_guest_rate_limit():
    """Credential-shaped garbage cannot select the authenticated bucket."""
    app = _make_app()
    with app.test_request_context(headers={"Authorization": "Bearer fake"}), patch(
        "reana_server.factory.validate_access_token",
        side_effect=InvalidTokenError("invalid"),
    ):
        assert _set_rate_limit() == _GUEST_LIMIT


def test_auth_backend_error_uses_guest_rate_limit():
    """Authentication cache failures do not turn rate selection into a 500."""
    app = _make_app()
    with app.test_request_context(headers={"Authorization": "Bearer token"}), patch(
        "reana_server.factory.validate_access_token",
        side_effect=AuthError("issuer unavailable"),
    ):
        assert _set_rate_limit() == _GUEST_LIMIT


def test_rate_limit_classification_never_fetches_oidc_metadata():
    """A credential-shaped request cannot trigger discovery or JWKS I/O."""
    app = _make_app({"REANA_AUTH": {"issuer": "https://auth.example.org"}})
    token = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFueSJ9.e30.c2ln"
    with app.test_request_context(
        headers={"Authorization": f"Bearer {token}"}
    ), patch.object(tokens.requests, "get") as jwks_get, patch.object(
        discovery.requests, "get"
    ) as discovery_get:
        assert _set_rate_limit() == _GUEST_LIMIT
    jwks_get.assert_not_called()
    discovery_get.assert_not_called()


def test_fake_cookie_uses_guest_rate_limit():
    """An invalid BFF cookie remains in the guest bucket."""
    app = _make_app()
    with app.test_request_context(headers={"Cookie": "reana_at=fake"}), patch(
        "reana_server.factory.validate_access_token",
        side_effect=InvalidTokenError("invalid"),
    ):
        assert _set_rate_limit() == _GUEST_LIMIT


def test_guest_rate_limit_key_ignores_user_agent():
    """Changing an untrusted header cannot select a new guest bucket."""
    app = _make_app()
    with app.test_request_context(
        headers={"User-Agent": "first"}, environ_base={"REMOTE_ADDR": "192.0.2.1"}
    ):
        first = _rate_limit_key()
    with app.test_request_context(
        headers={"User-Agent": "second"}, environ_base={"REMOTE_ADDR": "192.0.2.1"}
    ):
        second = _rate_limit_key()

    assert first == second == "ip:192.0.2.1"


def test_authenticated_requests_key_by_identity_not_shared_address():
    """Two subjects behind one address do not share the authenticated counter."""
    app = _make_app()
    claims_by_token = {
        "token-a": {"iss": "https://auth.example.org", "sub": "alice"},
        "token-b": {"iss": "https://auth.example.org", "sub": "bob"},
    }
    with patch(
        "reana_server.factory.validate_access_token",
        side_effect=lambda token, allow_remote=False: claims_by_token[token],
    ):
        with app.test_request_context(
            headers={"Authorization": "Bearer token-a"},
            environ_base={"REMOTE_ADDR": "192.0.2.1"},
        ):
            key_a = _rate_limit_key()
        with app.test_request_context(
            headers={"Authorization": "Bearer token-b"},
            environ_base={"REMOTE_ADDR": "192.0.2.1"},
        ):
            key_b = _rate_limit_key()

    assert key_a == "user:https://auth.example.org:alice"
    assert key_b == "user:https://auth.example.org:bob"
    assert key_a != key_b


def test_proxyfix_selects_client_address_from_one_trusted_proxy():
    """Per-client limiting sees the address supplied by the chart ingress."""
    app = _make_app({"PROXYFIX_CONFIG": {"x_for": 1, "x_proto": 1}})

    @app.route("/client-address")
    def client_address():
        return _rate_limit_key()

    with app.test_client() as client:
        response = client.get(
            "/client-address",
            headers={"X-Forwarded-For": "192.0.2.44"},
            environ_base={"REMOTE_ADDR": "10.0.0.8"},
        )

    assert response.text == "ip:192.0.2.44"
