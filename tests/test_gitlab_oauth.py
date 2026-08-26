# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for GitLab OAuth transaction binding."""

from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from reana_server.oauth_state import GITLAB_STATE_COOKIE, _serializer


def test_gitlab_connect_binds_state_to_reana_user(app, user0, auth_headers):
    """The signed transaction records the REANA account being connected."""
    headers = auth_headers(user0)
    user_id = str(user0.id_)
    with patch(
        "reana_server.rest.gitlab.REANA_GITLAB_URL", "https://gitlab.example.org"
    ), app.test_client() as client:
        response = client.get("/api/gitlab/connect", headers=headers)
        cookie = client.get_cookie(GITLAB_STATE_COOKIE, path="/api")

    assert response.status_code == 302
    assert parse_qs(urlparse(response.headers["Location"]).query)["state"]
    with app.app_context(), app.test_request_context():
        state = _serializer().loads(cookie.value)
    assert state["flow"] == "gitlab"
    assert state["user_id"] == user_id


def test_gitlab_callback_rejects_a_different_reana_user(app, user0, auth_headers):
    """A GitLab code cannot be stored under an account that did not start it."""
    state_param = "gitlab-state"
    headers = auth_headers(user0)
    with app.app_context(), app.test_request_context():
        cookie = _serializer().dumps(
            {
                "state": state_param,
                "flow": "gitlab",
                "next": "/profile",
                "user_id": "another-reana-user",
            }
        )

    with app.test_client() as client:
        client.set_cookie(GITLAB_STATE_COOKIE, cookie, path="/api")
        with patch(
            "reana_server.rest.gitlab.GitLabClient.oauth_token"
        ) as exchange, patch(
            "reana_server.rest.gitlab.UserSecretsStore.fetch"
        ) as fetch_secrets:
            response = client.get(
                f"/api/gitlab?state={state_param}&code=gitlab-code",
                headers=headers,
            )

    assert response.status_code == 403
    exchange.assert_not_called()
    fetch_secrets.assert_not_called()
