# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for the FastAPI CORS restriction and HTTP security headers.

This is the FastAPI counterpart of the former ``test_ext.py``: it pins the
hardening introduced for the Flask app in PR #766 ("restrict CORS and add
HTTP security headers") so the migration to FastAPI keeps the same behaviour.
"""

import pytest
from fastapi.testclient import TestClient

from reana_server.asgi import app
from reana_server.config import REANA_URL

# The exact header set #766 guarantees, kept here independently of the
# application constant so a typo in either side is caught.
_EXPECTED_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "base-uri 'self'"
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


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.parametrize("header,value", _EXPECTED_SECURITY_HEADERS.items())
def test_security_headers_on_normal_response(client, header, value):
    """Each security header is set to the expected value on every response."""
    res = client.get("/api/ping")
    assert res.status_code == 200
    assert res.headers.get(header) == value


def test_cors_matching_origin_echoed_back(client):
    """A request from the REANA origin gets Access-Control-Allow-Origin echoed."""
    res = client.get("/api/ping", headers={"Origin": REANA_URL})
    assert res.headers.get("access-control-allow-origin") == REANA_URL


def test_cors_non_matching_origin_rejected(client):
    """A request from a different origin gets no Access-Control-Allow-Origin."""
    res = client.get("/api/ping", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in res.headers


def test_cors_never_wildcard(client):
    """The API must never answer with a wildcard Access-Control-Allow-Origin."""
    res = client.get("/api/ping", headers={"Origin": "https://evil.example.com"})
    assert res.headers.get("access-control-allow-origin") != "*"
