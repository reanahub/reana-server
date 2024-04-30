# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server GitLab client tests."""

import unittest.mock as mock
from uuid import uuid4

import pytest
from reana_commons.k8s.secrets import UserSecrets, Secret

import reana_server.config as config
from reana_server.gitlab_client import GitLabClient, GitLabClientInvalidToken


def mock_response(status_code=200, json={}, content=b"", links={}):
    """Mock response."""
    response = mock.MagicMock()
    response.status_code = status_code
    response.content = content
    response.json.return_value = json
    response.links = links
    return response


def test_gitlab_client_from_k8s_secret():
    """Test creating authenticated GitLab client from user k8s secret."""
    user_id = uuid4()

    mock_fetch = mock.Mock()
    mock_fetch.return_value = UserSecrets(
        user_id=str(user_id),
        k8s_secret_name="gitlab_token",
        secrets=[Secret(name="gitlab_access_token", type_="env", value="gitlab_token")],
    )
    with mock.patch("reana_commons.k8s.secrets.UserSecretsStore.fetch", mock_fetch):
        gitlab_client = GitLabClient.from_k8s_secret(user_id, host="gitlab.example.org")
    assert gitlab_client.access_token == "gitlab_token"
    assert gitlab_client.host == "gitlab.example.org"


def test_gitlab_client_from_k8s_secret_invalid_token():
    """Test creating authenticated GitLab client when secret is not available."""
    user_id = uuid4()
    mock_fetch = mock.Mock()
    mock_fetch.return_value = UserSecrets(
        user_id=str(user_id),
        k8s_secret_name="k8s-secret-name",
        secrets=[],
    )
    with mock.patch("reana_commons.k8s.secrets.UserSecretsStore.fetch", mock_fetch):
        with pytest.raises(GitLabClientInvalidToken):
            GitLabClient.from_k8s_secret(user_id)


def test_gitlab_client_oauth_token():
    """Test getting OAuth token from GitLab."""
    response = mock_response()

    def request(verb, url, params, data):
        assert verb == "POST"
        assert url == "https://gitlab.example.org/oauth/token"
        assert params is None
        assert data == {"foo": "bar"}

        return response

    gitlab_client = GitLabClient(
        access_token="gitlab_token", host="gitlab.example.org", http_request=request
    )

    res = gitlab_client.oauth_token(data={"foo": "bar"})
    assert res is response


def test_gitlab_client_get_file():
    """Test getting file from GitLab."""

    def request(verb, url, params, data):
        assert verb == "GET"
        assert (
            url == "https://gitlab.example.org/api/v4/"
            "projects/123/repository/files/a.txt/raw"
        )
        assert params == {"access_token": "gitlab_token", "ref": "feature-branch"}
        assert data is None

        return mock_response(200, content=b"file content")

    gitlab_client = GitLabClient(
        access_token="gitlab_token", host="gitlab.example.org", http_request=request
    )

    res = gitlab_client.get_file(project=123, file_path="a.txt", ref="feature-branch")
    assert res.content == b"file content"


def test_gitlab_client_get_projects():
    """Test getting projects from GitLab."""

    response = mock_response()

    def request(verb, url, params, data):
        assert verb == "GET"
        assert url == "https://gitlab.example.org/api/v4/projects"
        assert params == {"access_token": "gitlab_token", "page": 123, "per_page": 20}
        assert data is None

        return response

    gitlab_client = GitLabClient(
        access_token="gitlab_token", host="gitlab.example.org", http_request=request
    )

    res = gitlab_client.get_projects(page=123, per_page=20)
    assert res is response


def test_gitlab_client_get_webhooks():
    """Test getting webhooks from GitLab."""
    response = mock_response()

    def request(verb, url, params, data):
        assert verb == "GET"
        assert url == "https://gitlab.example.org/api/v4/projects/123/hooks"
        assert params == {"access_token": "gitlab_token", "page": 123, "per_page": 20}
        assert data is None

        return response

    gitlab_client = GitLabClient(
        access_token="gitlab_token", host="gitlab.example.org", http_request=request
    )

    res = gitlab_client.get_webhooks(project=123, page=123, per_page=20)
    assert res is response


def test_gitlab_client_get_all_webhooks():
    """Test getting all webhooks from GitLab."""
    num_request = 0

    def request(verb, url, params, data):
        nonlocal num_request
        num_request += 1
        if num_request == 1:
            assert verb == "GET"
            assert url == "https://gitlab.example.org/api/v4/projects/123/hooks"
            assert params == {
                "access_token": "gitlab_token",
                "per_page": 100,
            }
            assert data is None
            return mock_response(json=[1, 2], links={"next": {"url": "second_url"}})
        elif num_request == 2:
            assert verb == "GET"
            assert url == "second_url"
            assert params is None
            assert data is None
            return mock_response(json=[3, 4], links={})
        else:
            assert False, "too many requests"

    gitlab_client = GitLabClient(
        access_token="gitlab_token", host="gitlab.example.org", http_request=request
    )

    res = gitlab_client.get_all_webhooks(project=123)
    assert list(res) == [1, 2, 3, 4]


def test_gitlab_client_create_webhook():
    """Test creating webhook in GitLab."""
    response = mock_response()

    def request(verb, url, params, data):
        assert verb == "POST"
        assert url == "https://gitlab.example.org/api/v4/projects/123/hooks"
        assert params == {
            "access_token": "gitlab_token",
        }
        assert data == {"xyz": "123"}

        return response

    gitlab_client = GitLabClient(
        access_token="gitlab_token", host="gitlab.example.org", http_request=request
    )

    res = gitlab_client.create_webhook(project=123, config={"xyz": "123"})
    assert res is response


def test_gitlab_client_delete_webhook():
    """Test deleting webhook in GitLab."""
    response = mock_response()

    def request(verb, url, params, data):
        assert verb == "DELETE"
        assert url == "https://gitlab.example.org/api/v4/projects/123/hooks/456"
        assert params == {
            "access_token": "gitlab_token",
        }
        assert data is None

        return response

    gitlab_client = GitLabClient(
        access_token="gitlab_token", host="gitlab.example.org", http_request=request
    )

    res = gitlab_client.delete_webhook(project=123, hook_id=456)
    assert res is response


def test_gitlab_client_set_commit_build_status():
    """Test setting commit build status in GitLab."""
    response = mock_response()

    def request(verb, url, params, data):
        assert verb == "POST"
        assert url == "https://gitlab.example.org/api/v4/projects/123/statuses/12345"
        assert params == {
            "access_token": "gitlab_token",
            "state": "success",
            "name": "custom_name",
            "description": "REANA workflow finished successfully",
        }
        assert data is None

        return response

    gitlab_client = GitLabClient(
        access_token="gitlab_token", host="gitlab.example.org", http_request=request
    )

    res = gitlab_client.set_commit_build_status(
        project=123,
        commit_sha="12345",
        state="success",
        name="custom_name",
        description="REANA workflow finished successfully",
    )
    assert res is response


def test_gitlab_client_get_user():
    """Test getting user from GitLab."""
    response = mock_response()

    def request(verb, url, params, data):
        assert verb == "GET"
        assert url == "https://gitlab.example.org/api/v4/user"
        assert params == {"access_token": "gitlab_token"}
        assert data is None

        return response

    gitlab_client = GitLabClient(
        access_token="gitlab_token", host="gitlab.example.org", http_request=request
    )

    res = gitlab_client.get_user()
    assert res is response
