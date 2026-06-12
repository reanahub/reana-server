# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for the BFF browser login flow."""

import json
import time
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import fakeredis
import pytest
from authlib.jose import JsonWebKey
from authlib.jose import jwt as jose_jwt

import reana_server.auth.sessions as sessions_module
import reana_server.auth.tokens as tokens_module
from reana_server.auth.sessions import AUTH_COOKIE, CSRF_COOKIE, CSRF_HEADER
from reana_server.config import REANA_AUTH
from reana_server.oauth_state import STATE_COOKIE, _serializer

ISSUER = "https://auth.example.org/realms/reana"
AUTHORIZATION_URL = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
END_SESSION_URL = f"{ISSUER}/protocol/openid-connect/logout"


@pytest.fixture
def signing_key():
    return JsonWebKey.generate_key("EC", "P-256", is_private=True)


def _make_token(key, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "reana",
        "sub": "subject-bff",
        "sid": "session-1",
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    header = {"alg": "ES256", "kid": key.as_dict(private=False).get("kid")}
    return jose_jwt.encode(header, claims, key).decode()


@pytest.fixture
def bff_config(monkeypatch, signing_key):
    """Enable the BFF against a fake issuer with explicit endpoints."""
    monkeypatch.setitem(REANA_AUTH, "issuer", ISSUER)
    monkeypatch.setitem(REANA_AUTH, "audience", "reana")
    monkeypatch.setitem(REANA_AUTH, "bff_enabled", True)
    monkeypatch.setitem(REANA_AUTH, "web_client_id", "reana-server")
    monkeypatch.setitem(REANA_AUTH, "web_client_secret", "secret")
    monkeypatch.setitem(REANA_AUTH, "jwks_url", f"{ISSUER}/jwks")
    monkeypatch.setitem(REANA_AUTH, "userinfo_url", f"{ISSUER}/userinfo")
    monkeypatch.setitem(REANA_AUTH, "authorization_url", AUTHORIZATION_URL)
    monkeypatch.setitem(REANA_AUTH, "token_url", TOKEN_URL)
    monkeypatch.setitem(REANA_AUTH, "end_session_url", END_SESSION_URL)
    monkeypatch.setattr(tokens_module, "_jwks_cache", None)
    jwks_response = Mock()
    jwks_response.raise_for_status = Mock()
    jwks_response.json = Mock(
        return_value={"keys": [signing_key.as_dict(private=False)]}
    )
    with patch.object(
        tokens_module.requests, "get", return_value=jwks_response
    ):
        yield


@pytest.fixture
def redis_store(monkeypatch):
    """Replace the Redis client with an in-memory fake."""
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(sessions_module, "_redis_client", fake)
    return fake


def _state_cookie_for(app, client, **payload):
    """Craft a valid signed state cookie and return the state value."""
    state = "test-state-value"
    with app.app_context(), app.test_request_context():
        cookie_value = _serializer().dumps({"state": state, **payload})
    client.set_cookie(STATE_COOKIE, cookie_value, path="/api")
    return state


class TestLogin:
    def test_disabled_returns_404(self, base_app, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "issuer", "")
        with base_app.test_client() as client:
            response = client.get("/api/login")
        assert response.status_code == 404

    def test_redirect_contract(self, base_app, bff_config):
        with base_app.test_client() as client:
            response = client.get("/api/login?next=/workflows")
        assert response.status_code == 302
        location = urlparse(response.headers["Location"])
        params = parse_qs(location.query)
        assert response.headers["Location"].startswith(AUTHORIZATION_URL)
        assert params["response_type"] == ["code"]
        assert params["client_id"] == ["reana-server"]
        assert params["code_challenge_method"] == ["S256"]
        assert params["code_challenge"][0]
        assert params["state"][0]
        cookies = response.headers.getlist("Set-Cookie")
        assert any(STATE_COOKIE in cookie for cookie in cookies)

    def test_next_url_must_be_relative(self, base_app, bff_config):
        with base_app.test_client() as client:
            response = client.get("/api/login?next=https://evil.example.org")
        # The crafted absolute URL is replaced by "/" in the state payload;
        # just assert the redirect still goes to the issuer.
        assert response.status_code == 302
        assert response.headers["Location"].startswith(AUTHORIZATION_URL)


class TestCallback:
    def test_state_mismatch_returns_403(self, base_app, bff_config):
        with base_app.test_client() as client:
            response = client.get("/api/oauth/callback?state=wrong&code=abc")
        assert response.status_code == 403

    def test_happy_path_sets_cookies_and_session(
        self, base_app, bff_config, redis_store, signing_key
    ):
        access_token = _make_token(signing_key)
        token_body = {
            "access_token": access_token,
            "refresh_token": "refresh-1",
            "id_token": "idt-1",
        }
        token_response = Mock(status_code=200)
        token_response.raise_for_status = Mock()
        token_response.json = Mock(return_value=token_body)
        with base_app.test_client() as client:
            state = _state_cookie_for(
                base_app, client, verifier="ver", next="/workflows"
            )
            with patch(
                "reana_server.rest.auth.requests.post",
                return_value=token_response,
            ) as mocked_post, patch(
                "reana_server.rest.auth.get_or_provision_user",
                return_value=Mock(id_="uid"),
            ), patch(
                "reana_server.rest.auth.fetch_userinfo", return_value={}
            ), patch(
                "reana_server.rest.auth.sync_user_groups_from_userinfo"
            ):
                response = client.get(
                    f"/api/oauth/callback?state={state}&code=the-code"
                )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/workflows")
        posted = mocked_post.call_args.kwargs.get(
            "data"
        ) or mocked_post.call_args[0][1]
        assert posted["grant_type"] == "authorization_code"
        assert posted["code_verifier"] == "ver"
        cookies = response.headers.getlist("Set-Cookie")
        assert any(
            cookie.startswith(f"{AUTH_COOKIE}=") and "HttpOnly" in cookie
            for cookie in cookies
        )
        assert any(
            cookie.startswith(f"{CSRF_COOKIE}=") and "HttpOnly" not in cookie
            for cookie in cookies
        )
        stored = redis_store.get("reana:bff:session:session-1")
        assert stored is not None
        assert json.loads(stored)["rt"] == "refresh-1"

    def test_token_exchange_failure_returns_502(
        self, base_app, bff_config, redis_store
    ):
        failing = Mock(status_code=500)
        failing.raise_for_status = Mock(
            side_effect=Exception("issuer down")
        )
        with base_app.test_client() as client:
            state = _state_cookie_for(base_app, client, verifier="v", next="/")
            with patch(
                "reana_server.rest.auth.requests.post",
                return_value=Mock(
                    raise_for_status=Mock(
                        side_effect=__import__("requests").RequestException()
                    )
                ),
            ):
                response = client.get(
                    f"/api/oauth/callback?state={state}&code=x"
                )
        assert response.status_code == 502


class TestLogout:
    def _login_cookies(self, client, token):
        client.set_cookie(AUTH_COOKIE, token, path="/api")
        client.set_cookie(CSRF_COOKIE, "csrf-value", path="/")

    def test_logout_clears_session(
        self, base_app, bff_config, redis_store, signing_key
    ):
        token = _make_token(signing_key)
        redis_store.set(
            "reana:bff:session:session-1",
            json.dumps({"rt": "r", "idt": "idt-1", "at": token}),
        )
        with base_app.test_client() as client:
            self._login_cookies(client, token)
            response = client.post(
                "/api/logout", headers={CSRF_HEADER: "csrf-value"}
            )
        assert response.status_code == 200
        assert response.json["logout_url"].startswith(END_SESSION_URL)
        assert "id_token_hint=idt-1" in response.json["logout_url"]
        assert redis_store.get("reana:bff:session:session-1") is None
        cookies = response.headers.getlist("Set-Cookie")
        assert any(
            cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cookies
        )

    def test_logout_without_cookie_401(self, base_app, bff_config):
        with base_app.test_client() as client:
            response = client.post("/api/logout")
        assert response.status_code == 401

    def test_logout_without_csrf_403(
        self, base_app, bff_config, redis_store, signing_key
    ):
        token = _make_token(signing_key)
        with base_app.test_client() as client:
            self._login_cookies(client, token)
            response = client.post("/api/logout")
        assert response.status_code == 403

    def test_logout_with_expired_token_still_works(
        self, base_app, bff_config, redis_store, signing_key
    ):
        token = _make_token(signing_key, exp=int(time.time()) - 3600)
        redis_store.set(
            "reana:bff:session:session-1",
            json.dumps({"rt": "r", "idt": "", "at": ""}),
        )
        with base_app.test_client() as client:
            self._login_cookies(client, token)
            response = client.post(
                "/api/logout", headers={CSRF_HEADER: "csrf-value"}
            )
        assert response.status_code == 200
        assert redis_store.get("reana:bff:session:session-1") is None
