# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test REANA-Server configuration helpers."""

import logging

import pytest

from reana_server.config import _get_int_env_variable


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
