# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2021, 2022, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test REANA-Server configuration helpers."""

import importlib.util
import json
import logging

import pytest

import reana_server.config as config
from reana_server.config import _get_int_env_variable


def _load_config_module():
    """Load a fresh copy of the config module using the current environment."""
    spec = importlib.util.spec_from_file_location(
        "reana_server_config_test", config.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, 100),
        ("", 100),
        ("not-an-integer", 100),
        ("1234", 1234),
    ],
)
def test_get_int_env_variable(monkeypatch, caplog, env_value, expected):
    """Test integer environment variable parsing with default fallback."""
    env_variable = "REANA_TEST_INT_ENV_VARIABLE"
    if env_value is None:
        monkeypatch.delenv(env_variable, raising=False)
    else:
        monkeypatch.setenv(env_variable, env_value)

    with caplog.at_level(logging.WARNING):
        assert _get_int_env_variable(env_variable, 100) == expected

    if env_value in {"", "not-an-integer"}:
        assert f"Invalid {env_variable}" in caplog.text
    else:
        assert f"Invalid {env_variable}" not in caplog.text


def test_keycloak_user_info_endpoint_is_enabled(monkeypatch):
    """Test that generic Keycloak SSO enables user info endpoint lookups."""
    issuer_url = "https://auth.example.org/auth/realms/example"
    login_providers_configs = [
        {
            "name": "test-1",
            "type": "keycloak",
            "config": {
                "title": "Test Provider",
                "base_url": issuer_url,
                "realm_url": issuer_url,
                "auth_url": f"{issuer_url}/protocol/openid-connect/auth",
                "token_url": f"{issuer_url}/protocol/openid-connect/token",
                "userinfo_url": f"{issuer_url}/protocol/openid-connect/userinfo",
            },
        }
    ]
    login_providers_secrets = {
        "test-1": {
            "consumer_key": "test-client-id",
            "consumer_secret": "test-client-secret",
        }
    }
    monkeypatch.setenv("LOGIN_PROVIDERS_CONFIGS", json.dumps(login_providers_configs))
    monkeypatch.setenv("LOGIN_PROVIDERS_SECRETS", json.dumps(login_providers_secrets))

    test_config = _load_config_module()

    assert test_config.OAUTHCLIENT_KEYCLOAK_USER_INFO_FROM_ENDPOINT is True
