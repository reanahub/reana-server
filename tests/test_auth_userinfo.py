# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for UserInfo fetch failure classification."""

from unittest.mock import Mock, patch

import pytest
import requests

from reana_server.auth.errors import IssuerUnavailableError, ProvisioningError
from reana_server.auth.userinfo import fetch_userinfo


def _json_response(payload, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.raise_for_status = Mock()
    response.json.return_value = payload
    return response


def test_userinfo_transport_failure_is_an_availability_error(base_app):
    """An issuer outage is a 502/503 availability error, not an auth denial."""
    with base_app.app_context():
        with patch(
            "reana_server.auth.userinfo.requests.get",
            side_effect=requests.RequestException("issuer down"),
        ):
            with pytest.raises(IssuerUnavailableError):
                fetch_userinfo("token")


def test_userinfo_http_error_is_an_availability_error(base_app):
    """A non-2xx UserInfo response is also an availability error."""
    response = _json_response({}, status_code=500)
    response.raise_for_status = Mock(side_effect=requests.HTTPError("500 Server Error"))
    with base_app.app_context():
        with patch("reana_server.auth.userinfo.requests.get", return_value=response):
            with pytest.raises(IssuerUnavailableError):
                fetch_userinfo("token")


def test_userinfo_missing_email_is_a_provisioning_error(base_app):
    """An unusable profile (no email) is a provisioning error, not availability."""
    with base_app.app_context():
        with patch(
            "reana_server.auth.userinfo.requests.get",
            return_value=_json_response({"sub": "abc"}),
        ):
            with pytest.raises(ProvisioningError, match="missing 'email'"):
                fetch_userinfo("token")
