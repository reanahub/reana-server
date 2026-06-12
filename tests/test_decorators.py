# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022, 2023, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server decorators tests."""

import json
import time
from unittest.mock import Mock, patch

import fakeredis
import pytest
from flask import jsonify

import reana_server.auth.sessions as sessions_module
from reana_server.auth.sessions import AUTH_COOKIE, CSRF_COOKIE, CSRF_HEADER
from reana_server.config import REANA_AUTH
from reana_server.decorators import signin_required
from tests.conftest import TEST_ISSUER


@pytest.fixture
def redis_store(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(sessions_module, "_redis_client", fake)
    return fake


def _ok_endpoint():
    return Mock(return_value=(jsonify(message="Ok"), 200))


class TestBearerAuthentication:
    def test_valid_bearer(self, app, user0, auth_headers):
        endpoint = _ok_endpoint()
        headers = auth_headers(user0)
        with app.test_request_context(headers=headers):
            response, code = signin_required()(endpoint)()
        assert code == 200
        endpoint.assert_called_once()
        assert endpoint.call_args.kwargs["user"].id_ == user0.id_

    def test_invalid_bearer_401(self, app):
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"Authorization": "Bearer garbage"}
        ):
            response, code = signin_required()(endpoint)()
        assert code == 401
        endpoint.assert_not_called()

    def test_missing_role_403(self, app, user0, auth_headers):
        endpoint = _ok_endpoint()
        headers = auth_headers(user0, roles=())
        with app.test_request_context(headers=headers):
            response, code = signin_required()(endpoint)()
        assert code == 403
        endpoint.assert_not_called()

    def test_missing_role_allowed_when_not_required(
        self, app, user0, auth_headers
    ):
        """token_required=False endpoints stay reachable without the role."""
        endpoint = _ok_endpoint()
        headers = auth_headers(user0, roles=())
        with app.test_request_context(headers=headers):
            response, code = signin_required(token_required=False)(endpoint)()
        assert code == 200

    def test_no_credentials_401(self, app):
        endpoint = _ok_endpoint()
        with app.test_request_context():
            response, code = signin_required()(endpoint)()
        assert code == 401
        message = json.loads(response.get_data(as_text=True))["message"]
        assert "not signed in" in message


class TestCookieAuthentication:
    def test_valid_cookie_get(
        self, app, user0, auth_headers, make_token, monkeypatch
    ):
        monkeypatch.setitem(REANA_AUTH, "bff_enabled", True)
        auth_headers(user0)  # links the idp identity
        token = make_token(user0.idp_subject)
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"Cookie": f"{AUTH_COOKIE}={token}"}
        ):
            response, code = signin_required()(endpoint)()
        assert code == 200

    def test_mutating_request_requires_csrf(
        self, app, user0, auth_headers, make_token, monkeypatch
    ):
        monkeypatch.setitem(REANA_AUTH, "bff_enabled", True)
        auth_headers(user0)
        token = make_token(user0.idp_subject)
        endpoint = _ok_endpoint()
        with app.test_request_context(
            method="POST",
            headers={"Cookie": f"{AUTH_COOKIE}={token}"},
        ):
            response, code = signin_required()(endpoint)()
        assert code == 403
        endpoint.assert_not_called()

    def test_mutating_request_with_csrf(
        self, app, user0, auth_headers, make_token, monkeypatch
    ):
        monkeypatch.setitem(REANA_AUTH, "bff_enabled", True)
        auth_headers(user0)
        token = make_token(user0.idp_subject)
        endpoint = _ok_endpoint()
        with app.test_request_context(
            method="POST",
            headers={
                "Cookie": f"{AUTH_COOKIE}={token}; {CSRF_COOKIE}=csrf-val",
                CSRF_HEADER: "csrf-val",
            },
        ):
            response, code = signin_required()(endpoint)()
        assert code == 200

    def test_expired_cookie_transparent_refresh(
        self, app, user0, auth_headers, make_token, monkeypatch, redis_store
    ):
        monkeypatch.setitem(REANA_AUTH, "bff_enabled", True)
        monkeypatch.setitem(REANA_AUTH, "token_url", f"{TEST_ISSUER}/token")
        auth_headers(user0)
        sid = "sid-refresh"
        expired = make_token(
            user0.idp_subject, sid=sid, exp=int(time.time()) - 3600
        )
        fresh = make_token(user0.idp_subject, sid=sid)
        redis_store.set(
            f"reana:bff:session:{sid}",
            json.dumps({"rt": "refresh-token", "idt": "", "at": ""}),
        )
        token_response = Mock(status_code=200)
        token_response.json = Mock(
            return_value={"access_token": fresh, "refresh_token": "rotated"}
        )
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"Cookie": f"{AUTH_COOKIE}={expired}"}
        ):
            with patch.object(
                sessions_module.requests, "post", return_value=token_response
            ):
                response, code = signin_required()(endpoint)()
        assert code == 200
        stored = json.loads(redis_store.get(f"reana:bff:session:{sid}"))
        assert stored["rt"] == "rotated"

    def test_expired_cookie_without_session_401(
        self, app, user0, auth_headers, make_token, monkeypatch, redis_store
    ):
        monkeypatch.setitem(REANA_AUTH, "bff_enabled", True)
        auth_headers(user0)
        expired = make_token(
            user0.idp_subject, sid="gone", exp=int(time.time()) - 3600
        )
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"Cookie": f"{AUTH_COOKIE}={expired}"}
        ):
            response, code = signin_required()(endpoint)()
        assert code == 401


class TestGitlabWebhookAuthentication:
    def test_valid_webhook_secret(self, app, session, user0):
        user0.gitlab_webhook_secret = "webhook-secret-value"
        session.commit()
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"X-Gitlab-Token": "webhook-secret-value"}
        ):
            response, code = signin_required(include_gitlab_login=True)(
                endpoint
            )()
        assert code == 200
        assert endpoint.call_args.kwargs["user"].id_ == user0.id_

    def test_invalid_webhook_secret_401(self, app, session, user0):
        user0.gitlab_webhook_secret = "webhook-secret-value"
        session.commit()
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"X-Gitlab-Token": "wrong-secret"}
        ):
            response, code = signin_required(include_gitlab_login=True)(
                endpoint
            )()
        assert code == 401

    def test_webhook_header_ignored_without_flag(self, app, session, user0):
        user0.gitlab_webhook_secret = "webhook-secret-value"
        session.commit()
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"X-Gitlab-Token": "webhook-secret-value"}
        ):
            response, code = signin_required()(endpoint)()
        assert code == 401
