# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for the FastAPI authentication layer: BFF cookie auth + login flow.

DB-free: the JWT is validated for real against a locally-served JWKS, Redis is
faked, and the single DB step (provisioning) is stubbed. HTTPS base URL so the
``Secure`` session cookies are honoured by the test client jar.
"""

import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fakeredis
import pytest
from authlib.jose import JsonWebKey
from authlib.jose import jwt as jose_jwt
from fastapi import FastAPI, Security
from fastapi.testclient import TestClient

import reana_server.auth.deps as deps
import reana_server.auth.sessions as sessions
import reana_server.auth.tokens as tokens_module
import reana_server.rest.auth as auth_rest
from reana_server.asgi import app
from reana_server.auth.deps import get_current_user
from reana_server.auth.sessions import AUTH_COOKIE, store_session
from reana_server.config import REANA_AUTH

ISSUER = "https://auth.example.org/realms/reana"
FAKE_USER = SimpleNamespace(
    id_="u1",
    email="jane.doe@example.org",
    full_name="Jane Doe",
    username="jdoe",
    idp_issuer=ISSUER,
    idp_subject="subject-1",
)


def _make_token(key, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "reana",
        "sub": "subject-1",
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    header = {"alg": "RS256", "kid": key.as_dict(private=False).get("kid")}
    return jose_jwt.encode(header, claims, key).decode()


def _jwks_response(key):
    response = Mock()
    response.raise_for_status = Mock()
    response.json = Mock(return_value={"keys": [key.as_dict(private=False)]})
    return response


@pytest.fixture
def signing_key():
    return JsonWebKey.generate_key("RSA", 2048, is_private=True)


@pytest.fixture
def bff_env(monkeypatch, signing_key):
    """Configure a trusted issuer with explicit endpoints + fake Redis."""
    for key, value in {
        "issuer": ISSUER,
        "audience": "reana",
        "jwks_url": f"{ISSUER}/jwks",
        "authorization_url": f"{ISSUER}/authorize",
        "token_url": f"{ISSUER}/token",
        "end_session_url": f"{ISSUER}/logout",
        "userinfo_url": f"{ISSUER}/userinfo",
        "web_client_id": "reana-server",
        "web_client_secret": "secret",
        "bff_enabled": True,
        "required_role": "reana:user",
    }.items():
        monkeypatch.setitem(REANA_AUTH, key, value)
    monkeypatch.setattr(tokens_module, "_jwks_cache", None)
    monkeypatch.setattr(
        sessions, "_redis_client", fakeredis.FakeStrictRedis(decode_responses=True)
    )
    monkeypatch.setattr(deps, "get_or_provision_user", lambda claims, token: FAKE_USER)
    with patch.object(
        tokens_module.requests, "get", return_value=_jwks_response(signing_key)
    ):
        yield signing_key


@pytest.fixture
def client():
    return TestClient(app, base_url="https://testserver")


# -- BFF cookie authentication (via a minimal app over get_current_user) ----


@pytest.fixture
def mini_client():
    mini = FastAPI()

    @mini.get("/g")
    async def _g(user=Security(get_current_user, scopes=[])):
        return {"email": user.email}

    @mini.post("/m")
    async def _m(user=Security(get_current_user, scopes=[])):
        return {"ok": True}

    return TestClient(mini, base_url="https://testserver")


def test_cookie_get_is_authenticated(mini_client, bff_env):
    token = _make_token(bff_env)
    response = mini_client.get("/g", cookies={AUTH_COOKIE: token})
    assert response.status_code == 200
    assert response.json()["email"] == FAKE_USER.email


def test_cookie_mutation_without_csrf_is_403(mini_client, bff_env):
    token = _make_token(bff_env)
    response = mini_client.post("/m", cookies={AUTH_COOKIE: token})
    assert response.status_code == 403


def test_cookie_mutation_with_csrf_is_200(mini_client, bff_env):
    token = _make_token(bff_env)
    response = mini_client.post(
        "/m",
        cookies={AUTH_COOKIE: token, "reana_csrf": "tok"},
        headers={"X-REANA-CSRF": "tok"},
    )
    assert response.status_code == 200


def test_expired_cookie_is_transparently_refreshed(mini_client, bff_env):
    expired = _make_token(bff_env, exp=int(time.time()) - 3600)
    store_session("subject-1", "refresh-1")  # session the refresh will rotate
    fresh = _make_token(bff_env)
    refresh_response = Mock(status_code=200)
    refresh_response.json = Mock(
        return_value={"access_token": fresh, "refresh_token": "refresh-2"}
    )
    with patch.object(sessions.requests, "post", return_value=refresh_response):
        response = mini_client.get("/g", cookies={AUTH_COOKIE: expired})
    assert response.status_code == 200
    # A fresh access cookie was re-issued.
    assert AUTH_COOKIE in response.headers.get("set-cookie", "")


# -- BFF login / logout / callback (via the real app) -----------------------


def test_login_redirects_to_issuer(client, bff_env):
    response = client.get(
        "/api/login", params={"next": "/dashboard"}, follow_redirects=False
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/authorize")
    assert "code_challenge=" in location and "state=" in location
    assert "reana_oauth_state=" in response.headers.get("set-cookie", "")


def test_logout_without_cookie_is_401(client, bff_env):
    assert client.post("/api/logout").status_code == 401


def test_logout_clears_session(client, bff_env):
    token = _make_token(bff_env)
    store_session("subject-1", "refresh-1", id_token="idt")
    response = client.post(
        "/api/logout",
        cookies={AUTH_COOKIE: token, "reana_csrf": "tok"},
        headers={"X-REANA-CSRF": "tok"},
    )
    assert response.status_code == 200
    assert response.json()["logout_url"].startswith(f"{ISSUER}/logout")
    assert sessions.get_session("subject-1") is None


def test_callback_establishes_session(client, bff_env, monkeypatch):
    # Drive a real /login to mint a matching state cookie + state param.
    login = client.get(
        "/api/login", params={"next": "/dashboard"}, follow_redirects=False
    )
    state = login.headers["location"].split("state=")[1].split("&")[0]

    access_token = _make_token(bff_env)
    token_response = Mock()
    token_response.raise_for_status = Mock()
    token_response.json = Mock(
        return_value={
            "access_token": access_token,
            "refresh_token": "refresh-1",
            "id_token": "idt",
        }
    )
    monkeypatch.setattr(
        auth_rest, "get_or_provision_user", lambda claims, token: FAKE_USER
    )
    monkeypatch.setattr(
        auth_rest, "fetch_userinfo", lambda token: {"sub": "subject-1"}
    )
    monkeypatch.setattr(
        auth_rest, "sync_user_groups_from_userinfo", lambda user, ui: None
    )
    with patch.object(auth_rest.requests, "post", return_value=token_response):
        response = client.get(
            "/api/oauth/callback",
            params={"code": "abc", "state": state},
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"
    assert AUTH_COOKIE in response.headers.get("set-cookie", "")
    assert sessions.get_session("subject-1") is not None
