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

import pytest
import requests
from authlib.jose import JsonWebKey
from authlib.jose import jwt as jose_jwt
from flask import jsonify

import reana_server.auth.sessions as sessions_module
import reana_server.auth.tokens as tokens_module
from reana_server.rest.auth import _client_facing_endpoint_url
from reana_server.auth.errors import (
    IssuerMisconfiguredError,
    IssuerUnavailableError,
    MissingRoleError,
    SessionUnavailableError,
)
from reana_server.auth.sessions import (
    AUTH_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    RefreshOutcome,
    SESSION_COOKIE,
)
from reana_server.decorators import signin_required
from reana_server.oauth_state import (
    BFF_STATE_COOKIE,
    GITLAB_STATE_COOKIE,
    STATE_COOKIE,
    _serializer,
)

ISSUER = "https://auth.example.org/realms/reana"
AUTHORIZATION_URL = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
END_SESSION_URL = f"{ISSUER}/protocol/openid-connect/logout"


def test_client_facing_endpoint_rewrites_different_backchannel_path(
    base_app, monkeypatch
):
    """Discovery relayed to clients must never expose the internal IdP URL."""
    auth = base_app.config["REANA_AUTH"]
    monkeypatch.setitem(auth, "issuer", "https://reana.example.org/auth/realms/reana")
    monkeypatch.setitem(
        auth,
        "backchannel_base_url",
        "http://keycloak:8080/keycloak/realms/reana",
    )

    with base_app.app_context():
        endpoint = _client_facing_endpoint_url(
            "http://keycloak:8080/keycloak/realms/reana/"
            "protocol/openid-connect/token?format=json"
        )

    assert endpoint == (
        "https://reana.example.org/auth/realms/reana/"
        "protocol/openid-connect/token?format=json"
    )


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


def _store_bound_session(sid, refresh_token, id_token, access_token):
    """Store a browser session with the identity used by this test module."""
    sessions_module.store_session(
        sid,
        refresh_token,
        id_token,
        access_token,
        issuer=ISSUER,
        subject="subject-bff",
        client_id="reana-server",
        created_at=time.time(),
    )


def _refresh_bound_session(sid, previous_access_token=""):
    """Refresh using the access-cookie identity used by this test module."""
    return sessions_module.refresh_session(
        sid,
        previous_access_token=previous_access_token,
        expected_issuer=ISSUER,
        expected_subject="subject-bff",
    )


@pytest.fixture
def bff_config(base_app, monkeypatch, signing_key):
    """Enable the BFF against a fake issuer with explicit endpoints."""
    auth = base_app.config["REANA_AUTH"]
    monkeypatch.setitem(auth, "issuer", ISSUER)
    monkeypatch.setitem(auth, "audience", ["reana"])
    monkeypatch.setitem(auth, "bff_enabled", True)
    monkeypatch.setitem(auth, "web_client_id", "reana-server")
    monkeypatch.setitem(auth, "web_client_secret", "secret")
    monkeypatch.setitem(auth, "jwks_url", f"{ISSUER}/jwks")
    monkeypatch.setitem(auth, "userinfo_url", f"{ISSUER}/userinfo")
    monkeypatch.setitem(auth, "authorization_url", AUTHORIZATION_URL)
    monkeypatch.setitem(auth, "token_url", TOKEN_URL)
    monkeypatch.setitem(auth, "end_session_url", END_SESSION_URL)
    base_app.extensions.pop(tokens_module._JWKS_EXTENSION, None)
    jwks_response = Mock()
    jwks_response.raise_for_status = Mock()
    jwks_response.json = Mock(
        return_value={"keys": [signing_key.as_dict(private=False)]}
    )
    with patch.object(tokens_module.requests, "get", return_value=jwks_response):
        with base_app.app_context():
            yield


def _state_cookie_for(app, client, **payload):
    """Craft a valid signed state cookie and return the state value."""
    state = "test-state-value"
    with app.app_context(), app.test_request_context():
        cookie_value = _serializer().dumps({"state": state, "flow": "bff", **payload})
    client.set_cookie(BFF_STATE_COOKIE, cookie_value, path="/api")
    return state


def test_count_sessions_excludes_refresh_locks(redis_store):
    """Status reports count browser sessions, not refresh lock keys."""
    redis_store.set("reana:bff:session:one", "{}")
    redis_store.set("reana:bff:session:one:lock", "1")
    redis_store.set("reana:bff:session:two", "{}")

    assert sessions_module.count_sessions() == 2


def test_count_sessions_reuses_a_recent_result_without_rescanning(redis_store):
    """Repeated calls within the TTL must not re-scan every session key."""
    redis_store.set("reana:bff:session:one", "{}")
    real_scan_iter = redis_store.scan_iter
    calls = []

    def counting_scan_iter(*args, **kwargs):
        calls.append(1)
        return real_scan_iter(*args, **kwargs)

    with patch.object(redis_store, "scan_iter", side_effect=counting_scan_iter):
        assert sessions_module.count_sessions() == 1
        assert sessions_module.count_sessions() == 1
        assert len(calls) == 1

        redis_store.set("reana:bff:session:two", "{}")
        # Still within the TTL: the new session must not be visible yet.
        assert sessions_module.count_sessions() == 1
        assert len(calls) == 1

    with patch(
        "reana_server.auth.sessions.time.monotonic",
        return_value=time.monotonic() + sessions_module._COUNT_SESSIONS_CACHE_TTL + 1,
    ):
        assert sessions_module.count_sessions() == 2


class TestLogin:
    def test_disabled_returns_404(self, base_app, monkeypatch):
        monkeypatch.setitem(base_app.config["REANA_AUTH"], "issuer", "")
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

    def test_redirect_preserves_authorization_endpoint_query(
        self, base_app, bff_config
    ):
        """Issuer-specific query parameters remain well-formed."""
        with base_app.test_client() as client, patch(
            "reana_server.rest.auth.get_endpoint",
            return_value=AUTHORIZATION_URL + "?kc_idp_hint=institution",
        ):
            response = client.get("/api/login")

        params = parse_qs(urlparse(response.headers["Location"]).query)
        assert response.status_code == 302
        assert params["kc_idp_hint"] == ["institution"]
        assert params["client_id"] == ["reana-server"]

    def test_login_does_not_overwrite_gitlab_state(self, base_app, bff_config):
        """The two OAuth flows retain independent in-flight transactions."""
        with base_app.test_client() as client:
            client.set_cookie(GITLAB_STATE_COOKIE, "existing-gitlab-state", path="/api")
            response = client.get("/api/login")

            assert response.status_code == 302
            assert client.get_cookie(GITLAB_STATE_COOKIE, path="/api").value == (
                "existing-gitlab-state"
            )
            assert client.get_cookie(BFF_STATE_COOKIE, path="/api") is not None

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

    def test_state_from_another_flow_returns_403(self, base_app, bff_config):
        """A correctly signed state cannot be consumed by the wrong flow."""
        state = "test-state-value"
        with base_app.app_context(), base_app.test_request_context():
            cookie_value = _serializer().dumps(
                {
                    "state": state,
                    "flow": "gitlab",
                    "verifier": "ver",
                    "next": "/",
                    "nonce": "nonce",
                }
            )
        with base_app.test_client() as client:
            client.set_cookie(BFF_STATE_COOKIE, cookie_value, path="/api")
            response = client.get(f"/api/oauth/callback?state={state}&code=code")

        assert response.status_code == 403

    def test_authorization_error_preserves_next_url_query_and_fragment(
        self, base_app, bff_config
    ):
        with base_app.test_client() as client:
            state = _state_cookie_for(
                base_app,
                client,
                verifier="ver",
                next="/signin?from=workflow#login",
            )
            response = client.get(
                f"/api/oauth/callback?state={state}&error=access_denied"
            )

        location = urlparse(response.headers["Location"])
        assert response.status_code == 302
        assert location.path == "/signin"
        assert parse_qs(location.query) == {
            "from": ["workflow"],
            "login_error": ["authorization"],
        }
        assert location.fragment == "login"

    def test_happy_path_sets_cookies_and_session(
        self, base_app, bff_config, redis_store, signing_key
    ):
        access_token = _make_token(signing_key)
        nonce = "callback-nonce"
        id_token = _make_token(signing_key, aud="reana-server", nonce=nonce)
        token_body = {
            "access_token": access_token,
            "refresh_token": "refresh-1",
            "id_token": id_token,
        }
        token_response = Mock(status_code=200)
        token_response.raise_for_status = Mock()
        token_response.json = Mock(return_value=token_body)
        with base_app.test_client() as client:
            state = _state_cookie_for(
                base_app,
                client,
                verifier="ver",
                next="/workflows",
                nonce=nonce,
            )
            with patch(
                "reana_server.rest.auth.requests.post",
                return_value=token_response,
            ) as mocked_post, patch(
                "reana_server.rest.auth.get_or_provision_user",
                return_value=(Mock(id_="uid"), False),
            ), patch(
                "reana_server.rest.auth.secrets.token_urlsafe",
                return_value="browser-session-id",
            ):
                response = client.get(
                    f"/api/oauth/callback?state={state}&code=the-code"
                )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/workflows")
        posted = mocked_post.call_args.kwargs.get("data") or mocked_post.call_args[0][1]
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
        assert any(
            cookie.startswith(f"{SESSION_COOKIE}=browser-session-id")
            and "HttpOnly" in cookie
            for cookie in cookies
        )
        stored = redis_store.get("reana:bff:session:browser-session-id")
        assert stored is not None
        assert json.loads(stored)["rt"] == "refresh-1"
        assert redis_store.get("reana:bff:session:session-1") is None

    @pytest.mark.parametrize(
        "id_token_factory",
        [
            lambda key, nonce: None,
            lambda key, nonce: _make_token(
                key, aud="reana-server", nonce="wrong-nonce"
            ),
        ],
    )
    def test_requires_valid_id_token_and_nonce(
        self,
        base_app,
        bff_config,
        redis_store,
        signing_key,
        id_token_factory,
    ):
        nonce = "expected-nonce"
        token_body = {
            "access_token": _make_token(signing_key),
            "refresh_token": "refresh-1",
        }
        id_token = id_token_factory(signing_key, nonce)
        if id_token:
            token_body["id_token"] = id_token
        token_response = Mock(status_code=200)
        token_response.raise_for_status = Mock()
        token_response.json = Mock(return_value=token_body)

        with base_app.test_client() as client:
            state = _state_cookie_for(
                base_app,
                client,
                verifier="ver",
                next="/",
                nonce=nonce,
            )
            with patch(
                "reana_server.rest.auth.requests.post",
                return_value=token_response,
            ):
                response = client.get(
                    f"/api/oauth/callback?state={state}&code=the-code"
                )

        assert response.status_code == 502
        assert not list(redis_store.scan_iter(match="reana:bff:session:*"))

    def test_token_exchange_failure_returns_502(
        self, base_app, bff_config, redis_store
    ):
        failing = Mock(status_code=500)
        failing.raise_for_status = Mock(side_effect=Exception("issuer down"))
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
                response = client.get(f"/api/oauth/callback?state={state}&code=x")
        assert response.status_code == 502

    def test_session_storage_failure_returns_503_without_auth_cookies(
        self, base_app, bff_config
    ):
        nonce = "storage-failure-nonce"
        token_response = Mock()
        token_response.raise_for_status = Mock()
        token_response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
        }
        with base_app.test_client() as client:
            state = _state_cookie_for(
                base_app, client, verifier="ver", next="/", nonce=nonce
            )
            with patch(
                "reana_server.rest.auth.requests.post",
                return_value=token_response,
            ), patch(
                "reana_server.rest.auth.validate_access_token",
                return_value={"iss": ISSUER, "sub": "subject"},
            ), patch(
                "reana_server.rest.auth.validate_id_token",
                return_value={"sub": "subject"},
            ), patch(
                "reana_server.rest.auth.get_or_provision_user",
                return_value=(Mock(), False),
            ), patch(
                "reana_server.rest.auth.store_session",
                side_effect=SessionUnavailableError("cache unavailable"),
            ):
                response = client.get(
                    f"/api/oauth/callback?state={state}&code=the-code"
                )

        assert response.status_code == 503
        cookies = response.headers.getlist("Set-Cookie")
        assert not any(cookie.startswith(f"{AUTH_COOKIE}=") for cookie in cookies)
        assert not any(cookie.startswith(f"{SESSION_COOKIE}=") for cookie in cookies)
        assert any(cookie.startswith(f"{STATE_COOKIE}=;") for cookie in cookies)

    def test_userinfo_outage_during_provisioning_returns_503(
        self, base_app, bff_config
    ):
        """A first-login UserInfo outage is an availability error, not a 500."""
        nonce = "provisioning-outage-nonce"
        token_response = Mock()
        token_response.raise_for_status = Mock()
        token_response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
        }
        with base_app.test_client() as client:
            state = _state_cookie_for(
                base_app, client, verifier="ver", next="/", nonce=nonce
            )
            with patch(
                "reana_server.rest.auth.requests.post",
                return_value=token_response,
            ), patch(
                "reana_server.rest.auth.validate_access_token",
                return_value={"iss": ISSUER, "sub": "subject"},
            ), patch(
                "reana_server.rest.auth.validate_id_token",
                return_value={"sub": "subject"},
            ), patch(
                "reana_server.rest.auth.get_or_provision_user",
                side_effect=IssuerUnavailableError("userinfo unavailable"),
            ):
                response = client.get(
                    f"/api/oauth/callback?state={state}&code=the-code"
                )

        assert response.status_code == 503
        cookies = response.headers.getlist("Set-Cookie")
        assert not any(cookie.startswith(f"{AUTH_COOKIE}=") for cookie in cookies)
        assert not any(cookie.startswith(f"{SESSION_COOKIE}=") for cookie in cookies)
        assert any(cookie.startswith(f"{STATE_COOKIE}=;") for cookie in cookies)


class TestLogout:
    def _login_cookies(self, client, token):
        client.set_cookie(AUTH_COOKIE, token, path="/api")
        client.set_cookie(SESSION_COOKIE, "browser-session-id", path="/api")
        client.set_cookie(CSRF_COOKIE, "csrf-value", path="/")

    def test_logout_clears_session(
        self, base_app, bff_config, redis_store, signing_key
    ):
        token = _make_token(signing_key)
        _store_bound_session("browser-session-id", "r", "idt-1", token)
        with base_app.test_client() as client:
            self._login_cookies(client, token)
            response = client.post("/api/logout", headers={CSRF_HEADER: "csrf-value"})
        assert response.status_code == 200
        assert response.json["logout_url"].startswith(END_SESSION_URL)
        assert "id_token_hint=idt-1" in response.json["logout_url"]
        assert redis_store.get("reana:bff:session:browser-session-id") is None
        cookies = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cookies)

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
        _store_bound_session("browser-session-id", "r", "", "")
        with base_app.test_client() as client:
            self._login_cookies(client, token)
            response = client.post("/api/logout", headers={CSRF_HEADER: "csrf-value"})
        assert response.status_code == 200
        assert redis_store.get("reana:bff:session:browser-session-id") is None

    def test_logout_does_not_destroy_mismatched_session(
        self, base_app, bff_config, redis_store, signing_key
    ):
        """Mixed cookies clear locally without deleting another identity's session."""
        token = _make_token(signing_key, sub="cookie-subject")
        sessions_module.store_session(
            "browser-session-id",
            "victim-refresh",
            "victim-id-token",
            "victim-access",
            issuer=ISSUER,
            subject="session-subject",
            client_id="reana-server",
            created_at=time.time(),
        )

        with base_app.test_client() as client:
            self._login_cookies(client, token)
            response = client.post("/api/logout", headers={CSRF_HEADER: "csrf-value"})

        assert response.status_code == 200
        assert response.json["logout_url"] == ""
        assert sessions_module.get_session("browser-session-id")["rt"] == (
            "victim-refresh"
        )
        assert "victim-id-token" not in response.get_data(as_text=True)
        cleared = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cleared)
        assert any(cookie.startswith(f"{SESSION_COOKIE}=;") for cookie in cleared)

    def test_session_storage_failure_returns_503_and_preserves_local_cookies(
        self, base_app, bff_config, signing_key
    ):
        token = _make_token(signing_key)
        with base_app.test_client() as client:
            self._login_cookies(client, token)
            with patch(
                "reana_server.rest.auth.get_session",
                side_effect=SessionUnavailableError("cache unavailable"),
            ):
                response = client.post(
                    "/api/logout", headers={CSRF_HEADER: "csrf-value"}
                )

        assert response.status_code == 503
        assert response.headers.getlist("Set-Cookie") == []

    @pytest.mark.parametrize(
        "error,status",
        [
            (IssuerUnavailableError("issuer unavailable"), 503),
            (IssuerMisconfiguredError("issuer misconfigured"), 500),
        ],
    )
    def test_token_validation_failure_preserves_retryable_logout(
        self, base_app, bff_config, signing_key, error, status
    ):
        token = _make_token(signing_key)
        with base_app.test_client() as client:
            self._login_cookies(client, token)
            with patch(
                "reana_server.rest.auth.decode_expired_token", side_effect=error
            ):
                response = client.post(
                    "/api/logout", headers={CSRF_HEADER: "csrf-value"}
                )

        assert response.status_code == status
        assert response.headers.getlist("Set-Cookie") == []

    def test_issuer_outage_still_allows_bound_local_logout(
        self, base_app, bff_config, redis_store, signing_key
    ):
        """Stored access-token equality safely binds cookies without JWKS."""
        token = _make_token(signing_key)
        _store_bound_session("browser-session-id", "r", "idt", token)
        with base_app.test_client() as client:
            self._login_cookies(client, token)
            with patch(
                "reana_server.rest.auth.decode_expired_token",
                side_effect=IssuerUnavailableError("issuer unavailable"),
            ):
                response = client.post(
                    "/api/logout", headers={CSRF_HEADER: "csrf-value"}
                )

        assert response.status_code == 200
        assert response.json["logout_url"] == ""
        assert redis_store.get("reana:bff:session:browser-session-id") is None
        assert any(
            cookie.startswith(f"{AUTH_COOKIE}=;")
            for cookie in response.headers.getlist("Set-Cookie")
        )

    def test_logout_url_failure_does_not_undo_completed_local_logout(
        self, base_app, bff_config, redis_store, signing_key
    ):
        token = _make_token(signing_key)
        _store_bound_session("browser-session-id", "r", "idt-1", token)
        with base_app.test_client() as client:
            self._login_cookies(client, token)
            with patch(
                "reana_server.rest.auth.get_endpoint",
                side_effect=IssuerUnavailableError("issuer unavailable"),
            ):
                response = client.post(
                    "/api/logout", headers={CSRF_HEADER: "csrf-value"}
                )

        assert response.status_code == 200
        assert response.json["logout_url"] == ""
        assert redis_store.get("reana:bff:session:browser-session-id") is None
        cookies = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cookies)


class TestRefreshSession:
    def test_refresh_lock_ttl_covers_http_and_redis_timeouts(self):
        auth_config = {
            "http_timeout": 12,
            "redis_socket_timeout": 7,
            "redis_socket_connect_timeout": 4,
        }

        assert sessions_module._refresh_lock_ttl(auth_config) == 75

    def test_refresh_lock_is_released_by_its_owner(self, redis_store):
        lock_key = "reana:bff:session:normal-release:lock"

        lock_owner = sessions_module._redis_set_refresh_lock(redis_store, lock_key, 10)

        assert lock_owner
        assert redis_store.get(lock_key) == lock_owner
        assert sessions_module._release_refresh_lock(redis_store, lock_key, lock_owner)
        assert redis_store.get(lock_key) is None

    def test_expired_lock_owner_cannot_delete_reacquired_lock(self, redis_store):
        lock_key = "reana:bff:session:reacquired:lock"
        old_owner = sessions_module._redis_set_refresh_lock(redis_store, lock_key, 10)
        new_owner = "new-request-owner"
        # Simulate expiry followed by another request acquiring the same key.
        redis_store.set(lock_key, new_owner, ex=10)

        assert not sessions_module._release_refresh_lock(
            redis_store, lock_key, old_owner
        )
        assert redis_store.get(lock_key) == new_owner

    @pytest.mark.parametrize(
        "error_code,session_is_deleted",
        [("invalid_grant", True), ("temporarily_unavailable", False)],
    )
    def test_only_invalid_grant_deletes_session(
        self,
        error_code,
        session_is_deleted,
        bff_config,
        redis_store,
    ):
        """Transient OAuth errors preserve refresh credentials for retry."""
        _store_bound_session("browser-session-id", "refresh", "id", "expired")
        response = Mock(status_code=400)
        response.json.return_value = {"error": error_code}

        with patch.object(sessions_module.requests, "post", return_value=response):
            result = _refresh_bound_session("browser-session-id", "expired")

        expected_outcome = (
            RefreshOutcome.TERMINAL if session_is_deleted else RefreshOutcome.TRANSIENT
        )
        assert result.outcome is expected_outcome
        assert result.access_token is None
        assert (
            sessions_module.get_session("browser-session-id") is None
        ) is session_is_deleted

    def test_waiter_ignores_previous_access_token(
        self, bff_config, redis_store, monkeypatch
    ):
        """A concurrent refresh waiter does not return the expired token."""
        session_key = "reana:bff:session:browser-session-id"
        lock_key = f"{session_key}:lock"
        redis_store.set(
            session_key,
            json.dumps(
                {
                    "rt": "refresh",
                    "idt": "id",
                    "at": "expired",
                    "iss": ISSUER,
                    "sub": "subject-bff",
                    "cid": "reana-server",
                }
            ),
        )
        redis_store.set(lock_key, "1", ex=10)
        sleeps = []

        def complete_refresh(_delay):
            sleeps.append(_delay)
            if len(sleeps) == 2:
                redis_store.set(
                    session_key,
                    json.dumps(
                        {
                            "rt": "rotated",
                            "idt": "id",
                            "at": "fresh",
                            "iss": ISSUER,
                            "sub": "subject-bff",
                            "cid": "reana-server",
                        }
                    ),
                )
                redis_store.delete(lock_key)

        monkeypatch.setattr(sessions_module.time, "sleep", complete_refresh)

        result = _refresh_bound_session("browser-session-id", "expired")

        assert result.outcome is RefreshOutcome.SUCCESS
        assert result.access_token == "fresh"
        assert len(sleeps) == 2

    def test_waiter_budget_is_independent_from_lock_ttl(
        self, base_app, bff_config, redis_store, monkeypatch
    ):
        """An abandoned long-lived lock cannot hold a request for its full TTL."""
        _store_bound_session("browser-session-id", "refresh", "id", "expired")
        lock_key = "reana:bff:session:browser-session-id:lock"
        redis_store.set(lock_key, "dead-owner", ex=60)
        monkeypatch.setitem(base_app.config["REANA_AUTH"], "refresh_wait_timeout", 0.25)
        clock = [0.0]

        monkeypatch.setattr(sessions_module.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(
            sessions_module.time,
            "sleep",
            lambda delay: clock.__setitem__(0, clock[0] + delay),
        )

        result = _refresh_bound_session("browser-session-id", "expired")

        assert result.outcome is RefreshOutcome.TRANSIENT
        assert clock[0] < 1
        assert redis_store.exists(lock_key)

    def test_malformed_refresh_response_is_transient(self, bff_config, redis_store):
        _store_bound_session("browser-session-id", "refresh", "id", "expired")
        response = Mock(status_code=200)
        response.json.return_value = {}

        with patch.object(sessions_module.requests, "post", return_value=response):
            result = _refresh_bound_session("browser-session-id", "expired")

        assert result.outcome is RefreshOutcome.TRANSIENT
        assert result.access_token is None

    @staticmethod
    def _decorated_response(app, expired, sid, csrf="csrf-stable"):
        headers = {
            "Cookie": (
                f"{AUTH_COOKIE}={expired}; {SESSION_COOKIE}={sid}; "
                f"{CSRF_COOKIE}={csrf}"
            )
        }
        with app.test_request_context(headers=headers):
            endpoint = Mock(return_value=(jsonify(message="Ok"), 200))
            result = signin_required()(endpoint)()
            response = app.make_response(result)
            return app.process_response(response), endpoint

    def test_transient_issuer_failure_returns_503_and_preserves_session(
        self, base_app, bff_config, redis_store, signing_key
    ):
        sid = "transient-issuer"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        _store_bound_session(sid, "refresh", "id", expired)

        with patch.object(
            sessions_module.requests,
            "post",
            side_effect=requests.ConnectionError("issuer unavailable"),
        ):
            response, endpoint = self._decorated_response(base_app, expired, sid)

        assert response.status_code == 503
        assert sessions_module.get_session(sid)["rt"] == "refresh"
        assert not response.headers.getlist("Set-Cookie")
        endpoint.assert_not_called()

    def test_jwks_rotation_outage_preserves_recoverable_browser_session(
        self, base_app, bff_config, redis_store, signing_key
    ):
        """An unavailable new signing key is a 503, not terminal cookie loss."""
        tokens_module.validate_access_token(_make_token(signing_key))
        sid = "rotation-outage"
        rotated_key = JsonWebKey.generate_key("EC", "P-256", is_private=True)
        expired = _make_token(rotated_key, exp=int(time.time()) - 3600)
        _store_bound_session(sid, "refresh", "id", expired)

        with patch.object(
            tokens_module.requests,
            "get",
            side_effect=requests.ConnectionError("issuer unavailable"),
        ):
            response, endpoint = self._decorated_response(base_app, expired, sid)

        assert response.status_code == 503
        assert sessions_module.get_session(sid)["rt"] == "refresh"
        assert not response.headers.getlist("Set-Cookie")
        endpoint.assert_not_called()

    def test_jwks_rotation_during_unknown_kid_backoff_preserves_session(
        self, base_app, bff_config, redis_store, signing_key
    ):
        """An ambiguous rotated key returns 503 without clearing cookies."""
        tokens_module.validate_access_token(_make_token(signing_key))
        cache = tokens_module._get_jwks_cache()
        cache.get_key_set_for_kid("attacker-controlled-kid")

        sid = "rotation-backoff"
        rotated_key = JsonWebKey.generate_key("EC", "P-256", is_private=True)
        expired = _make_token(rotated_key, exp=int(time.time()) - 3600)
        _store_bound_session(sid, "refresh", "id", expired)

        response, endpoint = self._decorated_response(base_app, expired, sid)

        assert response.status_code == 503
        assert sessions_module.get_session(sid)["rt"] == "refresh"
        assert not response.headers.getlist("Set-Cookie")
        endpoint.assert_not_called()

    def test_redis_timeout_returns_503_without_clearing_cookies(
        self, base_app, bff_config, redis_store, signing_key
    ):
        sid = "redis-timeout"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        _store_bound_session(sid, "refresh", "id", expired)

        with patch.object(
            redis_store,
            "set",
            side_effect=sessions_module.redis.TimeoutError("cache stalled"),
        ):
            response, endpoint = self._decorated_response(base_app, expired, sid)

        assert response.status_code == 503
        assert sessions_module.get_session(sid)["rt"] == "refresh"
        assert not response.headers.getlist("Set-Cookie")
        endpoint.assert_not_called()

    def test_expired_cookie_without_session_cookie_preserves_message_and_clears(
        self, base_app, bff_config, signing_key
    ):
        """An expired access cookie with no session cookie is a terminal 401.

        Regression test: this path must raise the same _TerminalSessionError
        used by RefreshOutcome.TERMINAL, not a plain InvalidTokenError -- the
        wrapper's generic InvalidTokenError handler discards the message and
        always returns "Invalid access token.", which reana-ui's client-side
        session-expiry detector does not recognise as a sign-out trigger,
        leaving the user stuck signed-in-but-broken (this is the single most
        routine expiry path: the session cookie has simply expired/been
        cleared while the access cookie is still present).
        """
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        headers = {
            "Cookie": f"{AUTH_COOKIE}={expired}; {CSRF_COOKIE}=csrf-stable",
        }
        with base_app.test_request_context(headers=headers):
            endpoint = Mock(return_value=(jsonify(message="Ok"), 200))
            result = signin_required()(endpoint)()
            response = base_app.process_response(base_app.make_response(result))

        assert response.status_code == 401
        assert response.get_json()["message"] == "Session expired, please log in again."
        cleared = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cleared)
        endpoint.assert_not_called()

    def test_bad_signature_cookie_is_terminal_and_clears_cookies(
        self, base_app, bff_config, signing_key
    ):
        """A tampered/forged access cookie is a terminal 401, not a wedge.

        Regression test for PR789-42: ``decode_expired_token``'s
        ``InvalidTokenError`` must be promoted to ``_TerminalSessionError`` so
        cookies are cleared -- the same as the no-session-cookie and
        ``RefreshOutcome.TERMINAL`` cases. Previously it propagated
        unconverted to the wrapper's generic ``except InvalidTokenError``
        clause, which returns a fixed "Invalid access token." message
        *without* clearing cookies, e.g. after a key rotation makes an old
        cookie's signature invalid: the browser then keeps presenting the
        same unusable cookie on every request with no way to recover except
        manually clearing cookies.
        """
        other_key = JsonWebKey.generate_key("EC", "P-256", is_private=True)
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": "reana",
            "sub": "subject-bff",
            "sid": "session-1",
            "iat": now,
            "exp": now + 600,
        }
        # Signed by a key other than the one advertised in the issuer's JWKS,
        # but claiming the real key's kid so it looks locally known and no
        # refetch is triggered.
        header = {"alg": "ES256", "kid": signing_key.as_dict(private=False).get("kid")}
        bad_signature_token = jose_jwt.encode(header, claims, other_key).decode()
        sid = "bad-signature-session"

        response, endpoint = self._decorated_response(
            base_app, bad_signature_token, sid
        )

        assert response.status_code == 401
        cleared = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cleared)
        endpoint.assert_not_called()

    def test_bad_issuer_cookie_is_terminal_and_clears_cookies(
        self, base_app, bff_config, signing_key
    ):
        """A cookie whose token claims a different issuer is a terminal 401.

        Regression test for PR789-42 (issuer reconfiguration case)."""
        token = _make_token(signing_key, iss="https://attacker.example/realms/reana")
        sid = "bad-issuer-session"

        response, endpoint = self._decorated_response(base_app, token, sid)

        assert response.status_code == 401
        cleared = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cleared)
        endpoint.assert_not_called()

    def test_bad_audience_cookie_is_terminal_and_clears_cookies(
        self, base_app, bff_config, signing_key
    ):
        """A cookie whose token claims an unconfigured audience is terminal.

        Regression test for PR789-42 (audience reconfiguration case)."""
        token = _make_token(signing_key, aud="not-reana")
        sid = "bad-audience-session"

        response, endpoint = self._decorated_response(base_app, token, sid)

        assert response.status_code == 401
        cleared = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cleared)
        endpoint.assert_not_called()

    def test_missing_sub_cookie_is_terminal_and_clears_cookies(
        self, base_app, bff_config, signing_key
    ):
        """A cookie whose token is missing 'sub' is a terminal 401.

        Regression test for PR789-42."""
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": "reana",
            "sid": "session-1",
            "iat": now,
            "exp": now + 600,
        }
        header = {"alg": "ES256", "kid": signing_key.as_dict(private=False).get("kid")}
        token = jose_jwt.encode(header, claims, signing_key).decode()
        sid = "missing-sub-session"

        response, endpoint = self._decorated_response(base_app, token, sid)

        assert response.status_code == 401
        cleared = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cleared)
        endpoint.assert_not_called()

    def test_invalid_grant_returns_401_deletes_session_and_clears_cookies(
        self, base_app, bff_config, redis_store, signing_key
    ):
        sid = "invalid-grant"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        _store_bound_session(sid, "refresh", "id", expired)
        token_response = Mock(status_code=400)
        token_response.json.return_value = {"error": "invalid_grant"}

        with patch.object(
            sessions_module.requests, "post", return_value=token_response
        ):
            response, endpoint = self._decorated_response(base_app, expired, sid)

        assert response.status_code == 401
        assert sessions_module.get_session(sid) is None
        cleared = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cleared)
        assert any(cookie.startswith(f"{SESSION_COOKIE}=;") for cookie in cleared)
        assert any(cookie.startswith(f"{CSRF_COOKIE}=;") for cookie in cleared)
        endpoint.assert_not_called()

    def test_mixed_access_and_session_cookies_are_rejected_without_refreshing(
        self, base_app, bff_config, redis_store, signing_key
    ):
        """An expired cookie cannot adopt another identity's refresh session."""
        sid = "subject-bff-session"
        victim_access_token = _make_token(signing_key)
        _store_bound_session(sid, "victim-refresh", "id", victim_access_token)
        foreign_expired = _make_token(
            signing_key,
            sub="different-subject",
            exp=int(time.time()) - 3600,
        )

        with patch.object(sessions_module.requests, "post") as refresh_request:
            response, endpoint = self._decorated_response(
                base_app, foreign_expired, sid
            )

        assert response.status_code == 401
        refresh_request.assert_not_called()
        # The current browser's mismatched cookies are cleared, but the valid
        # referenced session is not destroyed.
        assert sessions_module.get_session(sid)["rt"] == "victim-refresh"
        cleared = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}=;") for cookie in cleared)
        assert any(cookie.startswith(f"{SESSION_COOKIE}=;") for cookie in cleared)
        endpoint.assert_not_called()

    def test_successful_refresh_preserves_existing_csrf_cookie(
        self, base_app, bff_config, redis_store, signing_key
    ):
        sid = "refresh-success"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        fresh = _make_token(signing_key)
        _store_bound_session(sid, "refresh", "id", expired)
        token_response = Mock(status_code=200)
        token_response.json.return_value = {
            "access_token": fresh,
            "refresh_token": "rotated",
        }

        with patch(
            "reana_server.decorators._authorize_and_provision",
            return_value=Mock(),
        ), patch.object(sessions_module.requests, "post", return_value=token_response):
            response, endpoint = self._decorated_response(base_app, expired, sid)

        assert response.status_code == 200
        cookies = response.headers.getlist("Set-Cookie")
        assert any(cookie.startswith(f"{AUTH_COOKIE}={fresh}") for cookie in cookies)
        assert not any(cookie.startswith(f"{CSRF_COOKIE}=") for cookie in cookies)
        assert sessions_module.get_session(sid)["idt"] == "id"
        endpoint.assert_called_once()

    def test_role_revoked_after_refresh_installs_token_and_stops_rerefreshing(
        self, base_app, bff_config, redis_store, signing_key
    ):
        """A role removed mid-session refreshes once, then fails locally.

        Regression for the refresh loop: the refreshed token is installed on the
        403 so the browser stops presenting the expired one, and the following
        request is denied locally without a second issuer round trip.
        """
        sid = "role-revoked"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        fresh = _make_token(signing_key)
        _store_bound_session(sid, "refresh", "id", expired)
        token_response = Mock(status_code=200)
        token_response.json.return_value = {
            "access_token": fresh,
            "refresh_token": "rotated",
        }

        with patch(
            "reana_server.decorators._authorize_and_provision",
            side_effect=MissingRoleError("Role revoked."),
        ), patch.object(
            sessions_module.requests, "post", return_value=token_response
        ) as issuer_post:
            # First request: the expired token is refreshed, but the role is gone.
            first, first_endpoint = self._decorated_response(base_app, expired, sid)
            # The browser now presents the freshly installed token.
            second, second_endpoint = self._decorated_response(base_app, fresh, sid)

        assert first.status_code == 403
        first_cookies = first.headers.getlist("Set-Cookie")
        assert any(
            cookie.startswith(f"{AUTH_COOKIE}={fresh}") for cookie in first_cookies
        )
        # The second request validates the fresh token locally: still denied, but
        # with no further issuer call.
        assert second.status_code == 403
        assert issuer_post.call_count == 1
        first_endpoint.assert_not_called()
        second_endpoint.assert_not_called()

    def test_refresh_rejects_changed_subject(
        self, bff_config, redis_store, signing_key
    ):
        """A refreshed token cannot change the identity bound at login."""
        sid = "changed-subject"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        _store_bound_session(sid, "refresh", "id", expired)
        response = Mock(status_code=200)
        response.json.return_value = {
            "access_token": _make_token(signing_key, sub="different-subject")
        }

        with patch.object(sessions_module.requests, "post", return_value=response):
            result = _refresh_bound_session(sid, expired)

        assert result.outcome is RefreshOutcome.TERMINAL
        assert sessions_module.get_session(sid) is None

    def test_refresh_adopts_valid_refreshed_id_token(
        self, bff_config, redis_store, signing_key
    ):
        """A fresh ID token replaces the stale RP-initiated logout hint."""
        sid = "fresh-hint"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        _store_bound_session(sid, "refresh", "stale-id-token", expired)
        refreshed_id_token = _make_token(signing_key, aud="reana-server")
        response = Mock(status_code=200)
        response.json.return_value = {
            "access_token": _make_token(signing_key),
            "id_token": refreshed_id_token,
        }

        with patch.object(sessions_module.requests, "post", return_value=response):
            result = _refresh_bound_session(sid, expired)

        assert result.outcome is RefreshOutcome.SUCCESS
        assert sessions_module.get_session(sid)["idt"] == refreshed_id_token

    @pytest.mark.parametrize(
        "id_token_overrides",
        [{"sub": "someone-else"}, {"aud": "another-client"}],
        ids=["foreign-subject", "foreign-audience"],
    )
    def test_refresh_rejects_unconvincing_id_token(
        self, bff_config, redis_store, signing_key, id_token_overrides
    ):
        """A supplied refreshed ID token must remain bound to the session."""
        sid = "kept-hint"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        _store_bound_session(sid, "refresh", "login-id-token", expired)
        response = Mock(status_code=200)
        id_token_claims = {"aud": "reana-server", **id_token_overrides}
        response.json.return_value = {
            "access_token": _make_token(signing_key),
            "id_token": _make_token(signing_key, **id_token_claims),
        }

        with patch.object(sessions_module.requests, "post", return_value=response):
            result = _refresh_bound_session(sid, expired)

        assert result.outcome is RefreshOutcome.TERMINAL
        assert sessions_module.get_session(sid) is None

    def test_refresh_without_id_token_keeps_login_hint(
        self, bff_config, redis_store, signing_key
    ):
        """Issuers that omit the ID token on refresh keep the login-time one."""
        sid = "no-new-hint"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        _store_bound_session(sid, "refresh", "login-id-token", expired)
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": _make_token(signing_key)}

        with patch.object(sessions_module.requests, "post", return_value=response):
            result = _refresh_bound_session(sid, expired)

        assert result.outcome is RefreshOutcome.SUCCESS
        assert sessions_module.get_session(sid)["idt"] == "login-id-token"

    def test_refresh_rejects_unbound_legacy_session(self, bff_config, redis_store):
        """Sessions without issuer, subject, and client binding are ended."""
        sid = "unbound-session"
        redis_store.set(
            f"reana:bff:session:{sid}",
            json.dumps({"rt": "refresh", "idt": "id", "at": "expired"}),
        )

        result = _refresh_bound_session(sid, "expired")

        assert result.outcome is RefreshOutcome.TERMINAL
        assert sessions_module.get_session(sid) is None

    def test_refresh_caps_ttl_to_original_creation_window(
        self, base_app, bff_config, redis_store, signing_key, monkeypatch
    ):
        """A refresh near the end of the session window does not re-arm it.

        Without the fix, store_session() re-issues the full session_ttl on
        every refresh, so an actively used session never actually expires --
        contradicting the documented absolute cap. Set a short session_ttl,
        create a session whose original window is almost elapsed, refresh
        it, and assert the resulting Redis TTL is bounded by what remained
        of the *original* window, not reset to the full session_ttl.
        """
        monkeypatch.setitem(base_app.config["REANA_AUTH"], "session_ttl", 100)
        sid = "near-expiry"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        long_ago = time.time() - 95  # only ~5s left of a 100s window
        sessions_module.store_session(
            sid,
            "refresh",
            "id",
            expired,
            issuer=ISSUER,
            subject="subject-bff",
            client_id="reana-server",
            created_at=long_ago,
        )
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": _make_token(signing_key)}

        with patch.object(sessions_module.requests, "post", return_value=response):
            result = _refresh_bound_session(sid, expired)

        assert result.outcome is RefreshOutcome.SUCCESS
        ttl = redis_store.ttl(f"reana:bff:session:{sid}")
        assert 0 < ttl <= 10

    def test_refresh_well_within_window_extends_normally(
        self, base_app, bff_config, redis_store, signing_key, monkeypatch
    ):
        """A refresh soon after creation still gets close to the full TTL."""
        monkeypatch.setitem(base_app.config["REANA_AUTH"], "session_ttl", 100)
        sid = "fresh-session"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        sessions_module.store_session(
            sid,
            "refresh",
            "id",
            expired,
            issuer=ISSUER,
            subject="subject-bff",
            client_id="reana-server",
            created_at=time.time(),
        )
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": _make_token(signing_key)}

        with patch.object(sessions_module.requests, "post", return_value=response):
            result = _refresh_bound_session(sid, expired)

        assert result.outcome is RefreshOutcome.SUCCESS
        ttl = redis_store.ttl(f"reana:bff:session:{sid}")
        assert ttl > 90

    def test_refresh_finishing_after_absolute_deadline_is_discarded(
        self, base_app, bff_config, redis_store, signing_key, monkeypatch
    ):
        """A slow issuer response cannot resurrect an elapsed BFF session."""
        monkeypatch.setitem(base_app.config["REANA_AUTH"], "session_ttl", 100)
        sid = "deadline-crossed"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        redis_store.set(
            f"reana:bff:session:{sid}",
            json.dumps(
                {
                    "rt": "refresh",
                    "idt": "id",
                    "at": expired,
                    "iss": ISSUER,
                    "sub": "subject-bff",
                    "cid": "reana-server",
                    "created_at": 1000,
                }
            ),
        )
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": _make_token(signing_key)}

        clock = {"now": 1099}

        def finish_after_deadline(*_args, **_kwargs):
            clock["now"] = 1101
            return response

        with patch.object(
            sessions_module.requests, "post", side_effect=finish_after_deadline
        ), patch.object(sessions_module, "_now", side_effect=lambda: clock["now"]):
            result = sessions_module._refresh_locked_session(
                sid, base_app.config["REANA_AUTH"], ISSUER, "subject-bff"
            )

        assert result.outcome is RefreshOutcome.TERMINAL
        assert sessions_module.get_session(sid) is None

    def test_concurrent_revocation_during_refresh_does_not_resurrect_session(
        self, base_app, bff_config, redis_store, signing_key
    ):
        """An admin revocation racing an in-flight refresh must not be undone.

        The refresh lock only serializes concurrent refreshes of the same
        session against each other -- it is never checked by
        ``delete_sessions_for_subject`` (what ``reana-admin revoke-identity``
        calls). Without ``require_existing`` on the refresh path's final
        write, a revocation landing while the issuer round-trip is in
        flight would be silently undone once that round-trip completes.
        """
        sid = "revoked-mid-refresh"
        expired = _make_token(signing_key, exp=int(time.time()) - 3600)
        _store_bound_session(sid, "refresh", "id", expired)
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": _make_token(signing_key)}

        def revoke_during_issuer_call(*_args, **_kwargs):
            sessions_module.delete_sessions_for_subject(ISSUER, "subject-bff")
            return response

        with patch.object(
            sessions_module.requests, "post", side_effect=revoke_during_issuer_call
        ):
            result = _refresh_bound_session(sid, expired)

        assert result.outcome is RefreshOutcome.TERMINAL
        assert sessions_module.get_session(sid) is None
        lock_key = f"reana:bff:session:{sid}:lock"
        assert redis_store.get(lock_key) is None


class TestStoreSession:
    """Direct coverage of store_session's require_existing semantics."""

    def test_require_existing_returns_false_when_missing(self, redis_store):
        """XX must not create a session that was never (or no longer) stored."""
        stored = sessions_module.store_session(
            "never-stored",
            "refresh",
            issuer=ISSUER,
            subject="subject-bff",
            client_id="reana-server",
            created_at=time.time(),
            require_existing=True,
        )
        assert stored is False
        assert redis_store.get("reana:bff:session:never-stored") is None

    def test_require_existing_replaces_existing_session(self, redis_store):
        """XX must still replace an existing session's contents on the happy path."""
        sid = "already-stored"
        _store_bound_session(sid, "old-refresh", "old-id", "old-access")
        stored = sessions_module.store_session(
            sid,
            "new-refresh",
            "new-id",
            "new-access",
            issuer=ISSUER,
            subject="subject-bff",
            client_id="reana-server",
            created_at=time.time(),
            require_existing=True,
        )
        assert stored is True
        assert json.loads(redis_store.get(f"reana:bff:session:{sid}"))["rt"] == (
            "new-refresh"
        )


class TestDeleteSessionsForSubject:
    def test_deletes_only_the_matching_identity(self, redis_store):
        """Only the targeted issuer/subject's sessions are removed."""
        sessions_module.store_session(
            "target-1",
            "refresh",
            "id",
            "access",
            issuer=ISSUER,
            subject="target-subject",
            client_id="reana-server",
            created_at=time.time(),
        )
        sessions_module.store_session(
            "target-2",
            "refresh",
            "id",
            "access",
            issuer=ISSUER,
            subject="target-subject",
            client_id="reana-server",
            created_at=time.time(),
        )
        sessions_module.store_session(
            "other",
            "refresh",
            "id",
            "access",
            issuer=ISSUER,
            subject="other-subject",
            client_id="reana-server",
            created_at=time.time(),
        )

        count = sessions_module.delete_sessions_for_subject(ISSUER, "target-subject")

        assert count == 2
        assert sessions_module.get_session("target-1") is None
        assert sessions_module.get_session("target-2") is None
        assert sessions_module.get_session("other") is not None

    def test_dry_run_counts_without_deleting(self, redis_store):
        """A dry run reports the match count but deletes nothing."""
        sessions_module.store_session(
            "target-1",
            "refresh",
            "id",
            "access",
            issuer=ISSUER,
            subject="target-subject",
            client_id="reana-server",
            created_at=time.time(),
        )

        count = sessions_module.delete_sessions_for_subject(
            ISSUER, "target-subject", dry_run=True
        )

        assert count == 1
        assert sessions_module.get_session("target-1") is not None

    def test_ignores_refresh_lock_keys(self, redis_store):
        """A stray refresh-lock key never matches or gets deleted as a session."""
        sessions_module.store_session(
            "target-1",
            "refresh",
            "id",
            "access",
            issuer=ISSUER,
            subject="target-subject",
            client_id="reana-server",
            created_at=time.time(),
        )
        redis_store.set("reana:bff:session:target-1:lock", "1")

        count = sessions_module.delete_sessions_for_subject(ISSUER, "target-subject")

        assert count == 1
        assert redis_store.get("reana:bff:session:target-1:lock") == "1"
