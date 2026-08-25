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
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import fakeredis
import pytest
from flask import jsonify

import reana_server.auth.sessions as sessions_module
from reana_server.auth.errors import (
    IssuerKeyUnavailableError,
    IssuerMisconfiguredError,
    IssuerUnavailableError,
    UnknownKeyHealthyBackoffError,
)
from reana_server.auth.sessions import (
    AUTH_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    RefreshOutcome,
    RefreshResult,
    SESSION_COOKIE,
)
from reana_server.decorators import signin_required

TEST_ISSUER = "https://auth.example.org/realms/reana"


@pytest.fixture
def redis_store(app):
    fake = fakeredis.FakeRedis(decode_responses=True)
    app.extensions[sessions_module._REDIS_EXTENSION] = fake
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

    def test_bearer_scheme_is_case_insensitive(self, app, user0, auth_headers):
        """HTTP authentication schemes are matched case-insensitively."""
        endpoint = _ok_endpoint()
        authorization = auth_headers(user0)["Authorization"]
        headers = {"Authorization": authorization.replace("Bearer", "bearer", 1)}
        with app.test_request_context(headers=headers):
            response, code = signin_required()(endpoint)()
        assert code == 200
        endpoint.assert_called_once()

    def test_invalid_bearer_401(self, app):
        endpoint = _ok_endpoint()
        with app.test_request_context(headers={"Authorization": "Bearer garbage"}):
            response, code = signin_required()(endpoint)()
        assert code == 401
        assert json.loads(response.get_data(as_text=True))["message"] == (
            "Invalid access token."
        )
        endpoint.assert_not_called()

    def test_issuer_outage_is_reported_as_temporary_unavailability(self, app):
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"Authorization": "Bearer signed-token"}
        ), patch(
            "reana_server.decorators.validate_access_token",
            side_effect=IssuerUnavailableError("issuer unavailable"),
        ):
            response, code = signin_required()(endpoint)()

        assert code == 503
        assert (
            "temporarily unavailable"
            in json.loads(response.get_data(as_text=True))["message"]
        )
        endpoint.assert_not_called()

    def test_unknown_kid_healthy_backoff_is_401_not_503(self, app):
        """A one-shot bearer call gains nothing from being told to retry.

        Unlike the cookie/BFF path (a live user session, where a genuine key
        rotation arriving in this same window is more consequential to
        misclassify), the bearer path treats this narrow, healthy-cache case
        as an invalid credential rather than an issuer outage.
        """
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"Authorization": "Bearer signed-token"}
        ), patch(
            "reana_server.decorators.validate_access_token",
            side_effect=UnknownKeyHealthyBackoffError(
                "The token's signing key cannot currently be refreshed."
            ),
        ):
            response, code = signin_required()(endpoint)()

        assert code == 401
        endpoint.assert_not_called()

    def test_unknown_kid_failed_refresh_stays_503_on_bearer_path(self, app):
        """A genuinely failed refresh is not reclassified, even on the bearer path."""
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"Authorization": "Bearer signed-token"}
        ), patch(
            "reana_server.decorators.validate_access_token",
            side_effect=IssuerKeyUnavailableError(
                "The token's signing key cannot currently be refreshed."
            ),
        ):
            response, code = signin_required()(endpoint)()

        assert code == 503
        endpoint.assert_not_called()

    def test_issuer_misconfiguration_is_reported_as_500_not_403(self, app):
        """A discovery-config defect must not masquerade as role denial."""
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"Authorization": "Bearer signed-token"}
        ), patch(
            "reana_server.decorators.validate_access_token",
            side_effect=IssuerMisconfiguredError("discovery document is missing X"),
        ):
            response, code = signin_required()(endpoint)()

        assert code == 500
        assert (
            "not correctly configured"
            in json.loads(response.get_data(as_text=True))["message"]
        )
        endpoint.assert_not_called()

    def test_missing_role_403(self, app, user0, auth_headers):
        endpoint = _ok_endpoint()
        headers = auth_headers(user0, roles=())
        with app.test_request_context(headers=headers):
            response, code = signin_required()(endpoint)()
        assert code == 403
        endpoint.assert_not_called()

    def test_linked_user_missing_role_is_rejected_without_userinfo(
        self, app, user0, auth_headers
    ):
        """Role revocation takes effect locally for an already-linked user."""
        endpoint = _ok_endpoint()
        headers = auth_headers(user0, roles=())
        with patch("reana_server.auth.provision.fetch_userinfo") as fetch_userinfo:
            with app.test_request_context(headers=headers):
                response, code = signin_required()(endpoint)()
        assert code == 403
        fetch_userinfo.assert_not_called()
        endpoint.assert_not_called()

    def test_access_denied_code_is_machine_readable(self, app, user0, auth_headers):
        """The user bootstrap endpoint can expose the stable entitlement code."""
        endpoint = _ok_endpoint()
        headers = auth_headers(user0, roles=())
        with app.test_request_context(headers=headers):
            response, code = signin_required(access_denied_code="access_not_granted")(
                endpoint
            )()
        assert code == 403
        assert json.loads(response.get_data(as_text=True))["code"] == (
            "access_not_granted"
        )

    def test_no_credentials_401(self, app):
        endpoint = _ok_endpoint()
        with app.test_request_context():
            response, code = signin_required()(endpoint)()
        assert code == 401
        message = json.loads(response.get_data(as_text=True))["message"]
        assert "not signed in" in message


class TestCookieAuthentication:
    def test_valid_cookie_get(self, app, user0, auth_headers, make_token, monkeypatch):
        monkeypatch.setitem(app.config["REANA_AUTH"], "bff_enabled", True)
        auth_headers(user0)  # links the idp identity
        token = make_token(user0.idp_subject)
        endpoint = _ok_endpoint()
        with app.test_request_context(headers={"Cookie": f"{AUTH_COOKIE}={token}"}):
            response, code = signin_required()(endpoint)()
        assert code == 200

    def test_mutating_request_requires_csrf(
        self, app, user0, auth_headers, make_token, monkeypatch
    ):
        monkeypatch.setitem(app.config["REANA_AUTH"], "bff_enabled", True)
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
        monkeypatch.setitem(app.config["REANA_AUTH"], "bff_enabled", True)
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
        monkeypatch.setitem(app.config["REANA_AUTH"], "bff_enabled", True)
        monkeypatch.setitem(
            app.config["REANA_AUTH"], "token_url", f"{TEST_ISSUER}/token"
        )
        auth_headers(user0)
        sid = "sid-refresh"
        expired = make_token(user0.idp_subject, sid=sid, exp=int(time.time()) - 3600)
        fresh = make_token(user0.idp_subject, sid=sid)
        redis_store.set(
            f"reana:bff:session:{sid}",
            json.dumps(
                {
                    "rt": "refresh-token",
                    "idt": "",
                    "at": "",
                    "iss": TEST_ISSUER,
                    "sub": user0.idp_subject,
                    "cid": app.config["REANA_AUTH"]["web_client_id"],
                }
            ),
        )
        token_response = Mock(status_code=200)
        token_response.json = Mock(
            return_value={"access_token": fresh, "refresh_token": "rotated"}
        )
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"Cookie": (f"{AUTH_COOKIE}={expired}; " f"{SESSION_COOKIE}={sid}")}
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
        monkeypatch.setitem(app.config["REANA_AUTH"], "bff_enabled", True)
        auth_headers(user0)
        expired = make_token(user0.idp_subject, sid="gone", exp=int(time.time()) - 3600)
        endpoint = _ok_endpoint()
        with app.test_request_context(headers={"Cookie": f"{AUTH_COOKIE}={expired}"}):
            response, code = signin_required()(endpoint)()
        assert code == 401

    def test_unknown_kid_healthy_backoff_stays_503(
        self, app, user0, auth_headers, make_token, monkeypatch
    ):
        """The narrower bearer-only reclassification does not apply here.

        A genuine key rotation arriving in this same backoff window is more
        consequential to misclassify for a live user session than for a
        one-shot bearer API call, so the cookie/BFF path deliberately keeps
        this as an issuer-unavailable 503, not an invalid-token 401.
        """
        monkeypatch.setitem(app.config["REANA_AUTH"], "bff_enabled", True)
        auth_headers(user0)
        token = make_token(user0.idp_subject)
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"Cookie": f"{AUTH_COOKIE}={token}"}
        ), patch(
            "reana_server.decorators.validate_access_token",
            side_effect=UnknownKeyHealthyBackoffError(
                "The token's signing key cannot currently be refreshed."
            ),
        ):
            response, code = signin_required()(endpoint)()

        assert code == 503
        endpoint.assert_not_called()

    def test_refreshed_cookie_without_current_role_is_rejected(
        self, app, user0, auth_headers, make_token, monkeypatch
    ):
        """Role revocation takes effect when a BFF access token is refreshed."""
        monkeypatch.setitem(app.config["REANA_AUTH"], "bff_enabled", True)
        auth_headers(user0)
        sid = "sid-role-revoked"
        expired = make_token(user0.idp_subject, sid=sid, exp=int(time.time()) - 3600)
        refreshed_without_role = make_token(user0.idp_subject, sid=sid, roles=())
        endpoint = _ok_endpoint()

        with app.test_request_context(
            headers={"Cookie": f"{AUTH_COOKIE}={expired}; {SESSION_COOKIE}={sid}"}
        ), patch(
            "reana_server.decorators.refresh_session",
            return_value=RefreshResult(RefreshOutcome.SUCCESS, refreshed_without_role),
        ):
            response, code = signin_required()(endpoint)()

        assert code == 403
        endpoint.assert_not_called()


class TestGitlabWebhookAuthentication:
    def test_valid_webhook_secret(self, app, session, user0):
        user0.gitlab_webhook_secret = "webhook-secret-value"
        user0.gitlab_webhook_secret_expires_at = datetime.utcnow() + timedelta(hours=1)
        session.commit()
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"X-Gitlab-Token": "webhook-secret-value"}
        ):
            response, code = signin_required(include_gitlab_login=True)(endpoint)()
        assert code == 200
        assert endpoint.call_args.kwargs["user"].id_ == user0.id_

    def test_invalid_webhook_secret_401(self, app, session, user0):
        user0.gitlab_webhook_secret = "webhook-secret-value"
        user0.gitlab_webhook_secret_expires_at = datetime.utcnow() + timedelta(hours=1)
        session.commit()
        endpoint = _ok_endpoint()
        with app.test_request_context(headers={"X-Gitlab-Token": "wrong-secret"}):
            response, code = signin_required(include_gitlab_login=True)(endpoint)()
        assert code == 401

    def test_webhook_header_ignored_without_flag(self, app, session, user0):
        user0.gitlab_webhook_secret = "webhook-secret-value"
        user0.gitlab_webhook_secret_expires_at = datetime.utcnow() + timedelta(hours=1)
        session.commit()
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"X-Gitlab-Token": "webhook-secret-value"}
        ):
            response, code = signin_required()(endpoint)()
        assert code == 401

    @pytest.mark.parametrize("expiry", [None, datetime.utcnow() - timedelta(seconds=1)])
    def test_expired_webhook_secret_401(self, app, session, user0, expiry):
        """Missing and elapsed expiry timestamps fail closed."""
        user0.gitlab_webhook_secret = "webhook-secret-value"
        user0.gitlab_webhook_secret_expires_at = expiry
        session.commit()
        endpoint = _ok_endpoint()
        with app.test_request_context(
            headers={"X-Gitlab-Token": "webhook-secret-value"}
        ):
            response, code = signin_required(include_gitlab_login=True)(endpoint)()
        assert code == 401
        endpoint.assert_not_called()
