# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for the REANA Flask extension (security headers and CORS)."""

import pytest
from flask import Flask
from invenio_rest import InvenioREST

_TEST_ORIGIN = "https://example.com:30443"

_EXPECTED_SECURITY_HEADERS = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


@pytest.fixture
def ext_app():
    """Minimal Flask app with the REANA extension and CORS enabled."""
    from reana_server.ext import REANA

    app = Flask(__name__)
    app.config.update(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "REST_ENABLE_CORS": True,
            "REST_CSRF_ENABLED": False,
            "CORS_ORIGINS": [_TEST_ORIGIN],
            "CORS_SEND_WILDCARD": False,
            "CORS_SUPPORTS_CREDENTIALS": False,
        }
    )

    @app.route("/test")
    def test_route():
        return "OK"

    InvenioREST(app)
    REANA().init_app(app)
    return app


@pytest.mark.parametrize("header,value", _EXPECTED_SECURITY_HEADERS.items())
def test_security_headers_on_normal_response(ext_app, header, value):
    """Each security header is set to the expected value on every response."""
    with ext_app.test_client() as client:
        res = client.get("/test")
    assert res.headers.get(header) == value


def test_permissions_policy_present(ext_app):
    """Permissions-Policy header is present on every response."""
    with ext_app.test_client() as client:
        res = client.get("/test")
    assert "Permissions-Policy" in res.headers


def test_cors_matching_origin_echoed_back(ext_app):
    """A request from the allowed origin gets Access-Control-Allow-Origin echoed."""
    with ext_app.test_client() as client:
        res = client.get("/test", headers={"Origin": _TEST_ORIGIN})
    assert res.headers.get("Access-Control-Allow-Origin") == _TEST_ORIGIN


def test_cors_non_matching_origin_rejected(ext_app):
    """A request from a different origin does not get Access-Control-Allow-Origin."""
    with ext_app.test_client() as client:
        res = client.get("/test", headers={"Origin": "https://example.com"})
    assert "Access-Control-Allow-Origin" not in res.headers
