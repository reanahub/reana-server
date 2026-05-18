# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2023, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test users endpoints."""

from datetime import datetime
import json

from flask import url_for
from mock import patch
import pytest
from reana_commons.errors import REANAEmailNotificationError
from reana_db.models import AuditLogAction, User, UserTokenStatus


def test_get_users_shared_with_you(app, user1):
    """Test getting users who shared workflows with you."""
    with app.test_client() as client:
        response = client.get(
            url_for("users.get_users_shared_with_you"),
        )

        assert response.status_code == 401

        response = client.get(
            url_for("users.get_users_shared_with_you"),
            query_string={"access_token": "invalid_token"},
        )

        assert response.status_code == 403

        response = client.get(
            url_for("users.get_users_shared_with_you"),
            query_string={"access_token": user1.access_token},
        )

        assert response.status_code == 200


def test_get_users_you_shared_with(app, user1):
    """Test getting users who you shared workflows with."""
    with app.test_client() as client:
        response = client.get(
            url_for("users.get_users_you_shared_with"),
        )

        assert response.status_code == 401

        response = client.get(
            url_for("users.get_users_you_shared_with"),
            query_string={"access_token": "invalid_token"},
        )

        assert response.status_code == 403

        response = client.get(
            url_for("users.get_users_you_shared_with"),
            query_string={"access_token": user1.access_token},
        )

        assert response.status_code == 200


def test_get_you_includes_periodic_cpu_quota_metadata(app, session, user1):
    """Test authenticated user info includes periodic CPU quota metadata."""
    cpu_resource = next(
        resource
        for resource in user1.resources
        if resource.resource.type_.name == "cpu"
    )
    cpu_resource.quota_period_months = 3
    cpu_resource.quota_period_start_at = datetime(2026, 4, 1, 13, 6, 32, 992595)
    session.commit()

    with app.test_client() as client:
        response = client.get(
            url_for("users.get_you"),
            query_string={"access_token": user1.access_token},
        )

    assert response.status_code == 200
    assert response.json["quota"]["cpu"]["quota_period_months"] == 3
    assert (
        response.json["quota"]["cpu"]["quota_period_start_at"]
        == "2026-04-01T13:06:32.992595Z"
    )


def test_delete_token_requires_configuration(app, user1):
    """Test token management endpoint is disabled by default."""
    with app.test_client() as client:
        response = client.delete(
            url_for("users.delete_token"),
            headers={
                "Content-Type": "application/json",
                "X-Token-Management-Secret": "secret",
            },
            data=json.dumps({"email": user1.email}),
        )

    assert response.status_code == 403
    assert response.json["message"] == "Token management endpoint is not configured."
    assert response.json["status"] == 403


def test_delete_token_requires_valid_management_secret(app, user1):
    """Test token management endpoint rejects wrong secrets."""
    with app.test_client() as client:
        with patch("reana_server.rest.users.REANA_TOKEN_MANAGEMENT_SECRET", "secret"):
            response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "wrong-secret",
                },
                data=json.dumps({"email": user1.email}),
            )

    assert response.status_code == 401
    assert response.json["message"] == "Unauthorized"
    assert response.json["status"] == 401


def test_delete_token_fails_when_admin_user_cannot_be_resolved(app, session, user1):
    """Test token management endpoint refuses to proceed without an audit actor."""
    active_token = user1.access_token
    with app.test_client() as client:
        with patch(
            "reana_server.rest.users.REANA_TOKEN_MANAGEMENT_SECRET", "secret"
        ), patch(
            "reana_server.rest.users._get_admin_user_or_raise",
            side_effect=RuntimeError("Server misconfiguration."),
        ):
            response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "secret",
                },
                data=json.dumps({"email": user1.email}),
            )

    session.expire_all()
    refreshed_user = session.query(User).filter_by(id_=user1.id_).one()
    assert response.status_code == 500
    assert response.json["message"] == "Server misconfiguration."
    assert response.json["status"] == 500
    assert refreshed_user.access_token == active_token
    assert refreshed_user.access_token_status == UserTokenStatus.active.name


def test_delete_token_rejects_invalid_json_body(app):
    """Test token management endpoint validates JSON bodies."""
    with app.test_client() as client:
        with patch("reana_server.rest.users.REANA_TOKEN_MANAGEMENT_SECRET", "secret"):
            response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "secret",
                },
                data="not-json",
            )

    assert response.status_code == 400
    assert (
        response.json["message"] == "Invalid request. Expected application/json body."
    )
    assert response.json["status"] == 400


def test_delete_token_revokes_active_token(app, session, user0, user1):
    """Test token management endpoint revokes an active token."""
    revoked_token = user1.access_token
    with app.test_client() as client:
        with patch(
            "reana_server.rest.users.REANA_TOKEN_MANAGEMENT_SECRET", "secret"
        ), patch("reana_server.utils.REANAConfig.load", return_value={}), patch(
            "reana_server.utils.JinjaEnv.render_template", return_value="body"
        ), patch(
            "reana_server.utils.send_email"
        ) as send_email_mock:
            response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "secret",
                },
                data=json.dumps({"email": user1.email}),
            )

    session.expire_all()
    refreshed_user = session.query(User).filter_by(id_=user1.id_).one()
    assert response.status_code == 200
    assert response.json["email"] == user1.email
    assert response.json["message"] == "Access token revoked."
    assert response.json["status"] == 200
    assert response.json["reana_token"]["status"] == UserTokenStatus.revoked.name
    assert refreshed_user.access_token_status == UserTokenStatus.revoked.name
    assert user0.audit_logs[-1].action is AuditLogAction.revoke_token
    assert revoked_token not in user0.audit_logs[-1].details["reana_admin"]
    send_email_mock.assert_called_once()


def test_delete_token_revokes_active_token_by_user_id(app, session, user0, user1):
    """Test token management endpoint revokes a token identified by user ID."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.users.REANA_TOKEN_MANAGEMENT_SECRET", "secret"
        ), patch("reana_server.utils.REANAConfig.load", return_value={}), patch(
            "reana_server.utils.JinjaEnv.render_template", return_value="body"
        ), patch(
            "reana_server.utils.send_email"
        ) as send_email_mock:
            response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "secret",
                },
                data=json.dumps({"user_id": str(user1.id_)}),
            )

    session.expire_all()
    refreshed_user = session.query(User).filter_by(id_=user1.id_).one()
    assert response.status_code == 200
    assert response.json["id_"] == str(user1.id_)
    assert response.json["email"] == user1.email
    assert response.json["message"] == "Access token revoked."
    assert response.json["status"] == 200
    assert response.json["reana_token"]["status"] == UserTokenStatus.revoked.name
    assert refreshed_user.access_token_status == UserTokenStatus.revoked.name
    assert user0.audit_logs[-1].action is AuditLogAction.revoke_token
    send_email_mock.assert_called_once()


def test_delete_token_reports_success_when_email_fails(app, session, user0, user1):
    """Test token management endpoint keeps success response on email failure."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.users.REANA_TOKEN_MANAGEMENT_SECRET", "secret"
        ), patch("reana_server.utils.REANAConfig.load", return_value={}), patch(
            "reana_server.utils.JinjaEnv.render_template", return_value="body"
        ), patch(
            "reana_server.utils.send_email",
            side_effect=REANAEmailNotificationError("Email delivery failed."),
        ):
            response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "secret",
                },
                data=json.dumps({"email": user1.email}),
            )

    session.expire_all()
    refreshed_user = session.query(User).filter_by(id_=user1.id_).one()
    assert response.status_code == 200
    assert response.json["email"] == user1.email
    assert response.json["message"] == "Access token revoked."
    assert response.json["status"] == 200
    assert response.json["reana_token"]["status"] == UserTokenStatus.revoked.name
    assert refreshed_user.access_token_status == UserTokenStatus.revoked.name
    assert user0.audit_logs[-1].action is AuditLogAction.revoke_token


@pytest.mark.parametrize(
    "request_body",
    [
        {},
        {"email": "user1@reana.io", "user_id": "11111111-1111-1111-1111-111111111111"},
    ],
)
def test_delete_token_requires_exactly_one_identifier(app, request_body):
    """Test token management endpoint requires exactly one user identifier."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.users.REANA_TOKEN_MANAGEMENT_SECRET", "secret"
        ), patch(
            "reana_server.rest.users._get_user_by_criteria"
        ) as get_user_by_criteria_mock:
            response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "secret",
                },
                data=json.dumps(request_body),
            )

    assert response.status_code == 400
    assert (
        response.json["message"]
        == "Exactly one of `user_id` or `email` must be provided."
    )
    assert response.json["status"] == 400
    get_user_by_criteria_mock.assert_not_called()


def test_delete_token_rejects_unknown_user(app, user0):
    """Test token management endpoint hides whether the target user exists."""
    with app.test_client() as client:
        with patch("reana_server.rest.users.REANA_TOKEN_MANAGEMENT_SECRET", "secret"):
            response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "secret",
                },
                data=json.dumps({"email": "unknown@example.org"}),
            )

    assert response.status_code == 404
    assert response.json["message"] == "No active token to revoke for the given user."
    assert response.json["status"] == 404


def test_delete_token_rejects_user_without_active_token(app, session, user0):
    """Test token management endpoint hides missing active tokens."""
    user = User(email="requested.user@example.org")
    session.add(user)
    session.commit()
    user.request_access_token()

    with app.test_client() as client:
        with patch("reana_server.rest.users.REANA_TOKEN_MANAGEMENT_SECRET", "secret"):
            response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "secret",
                },
                data=json.dumps({"email": user.email}),
            )

    assert response.status_code == 404
    assert response.json["message"] == "No active token to revoke for the given user."
    assert response.json["status"] == 404


def test_delete_token_returns_same_response_for_unknown_and_inactive_users(
    app, session, user0
):
    """Test token management endpoint does not reveal whether a user exists."""
    user = User(email="inactive.user@example.org")
    session.add(user)
    session.commit()
    user.request_access_token()

    with app.test_client() as client:
        with patch("reana_server.rest.users.REANA_TOKEN_MANAGEMENT_SECRET", "secret"):
            unknown_response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "secret",
                },
                data=json.dumps({"email": "unknown@example.org"}),
            )
            inactive_response = client.delete(
                url_for("users.delete_token"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token-Management-Secret": "secret",
                },
                data=json.dumps({"email": user.email}),
            )

    assert unknown_response.status_code == 404
    assert inactive_response.status_code == 404
    assert unknown_response.json == inactive_response.json
