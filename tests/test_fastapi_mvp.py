# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""End-to-end tests for the FastAPI MVP (app + native JWT auth dependency).

No database or Kubernetes is required: the JWT is validated for real against
a locally-served JWKS (same pattern as ``test_auth.py``), and the single
DB-touching step (``get_or_provision_user``) is stubbed, so this suite
exercises the framework integration — routing, the OAuth2 scheme, workflow
controller proxying, the SecurityScopes role gate and the 401/403 mapping —
without the heavy app fixture.
"""

import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from authlib.jose import JsonWebKey
from authlib.jose import jwt as jose_jwt
from fastapi.testclient import TestClient

import reana_server.auth.deps as deps
import reana_server.auth.tokens as tokens_module
from reana_server.asgi import app
from reana_server.config import REANA_AUTH

ISSUER = "https://auth.example.org/realms/reana"


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
def client():
    return TestClient(app)


@pytest.fixture
def signing_key():
    return JsonWebKey.generate_key("RSA", 2048, is_private=True)


@pytest.fixture
def auth_env(monkeypatch, signing_key):
    """Trust a fake issuer, serve its JWKS, and stub JIT provisioning."""
    monkeypatch.setitem(REANA_AUTH, "issuer", ISSUER)
    monkeypatch.setitem(REANA_AUTH, "audience", "reana")
    monkeypatch.setitem(REANA_AUTH, "jwks_url", f"{ISSUER}/jwks")
    monkeypatch.setitem(REANA_AUTH, "required_role", "reana:user")
    monkeypatch.setattr(tokens_module, "_jwks_cache", None)
    # The only DB step in the dependency — return a fake user so no Postgres
    # is needed; the token validation and role gate around it run for real.
    fake_user = SimpleNamespace(
        id_="11111111-1111-1111-1111-111111111111",
        email="jane.doe@example.org",
        full_name="Jane Doe",
        username="jdoe",
        idp_issuer=ISSUER,
        idp_subject="subject-1",
    )
    monkeypatch.setattr(
        deps,
        "get_or_provision_user",
        lambda claims, token, userinfo=None: (fake_user, False),
    )
    with patch.object(
        tokens_module.requests, "get", return_value=_jwks_response(signing_key)
    ):
        yield signing_key


def test_ping_is_open(client):
    response = client.get("/api/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "200", "message": "OK"}


def test_openapi_advertises_oauth2(client):
    schema = client.get("/api/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert "OAuth2AuthorizationCodeBearer" in schemes
    assert schemes["OAuth2AuthorizationCodeBearer"]["type"] == "oauth2"
    # Listing is role-optional like the legacy endpoint.
    assert schema["paths"]["/api/workflows"]["get"]["security"] == [
        {"OAuth2AuthorizationCodeBearer": []}
    ]
    # Per-workflow reads remain role-gated.
    assert schema["paths"]["/api/workflows/{workflow_id_or_name}/status"]["get"][
        "security"
    ] == [
        {"OAuth2AuthorizationCodeBearer": ["reana:user"]}
    ]


def test_you_without_token_is_401(client):
    response = client.get("/api/you")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_you_with_garbage_token_is_401(client, auth_env):
    response = client.get(
        "/api/you", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401


def test_you_with_valid_token_is_200(client, auth_env):
    token = _make_token(auth_env)
    response = client.get(
        "/api/you", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "jane.doe@example.org"
    assert body["username"] == "jdoe"


class _FakeWorkflowControllerApi:
    def __init__(self, response=None, status_code=200):
        self.calls = []
        self._response = response or {"items": [], "total": 0}
        self._status_code = status_code

    def get_workflows(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            result=lambda: (
                self._response,
                SimpleNamespace(status_code=self._status_code),
            )
        )


def test_workflows_without_required_role_is_200(client, auth_env, monkeypatch):
    import reana_server.rest.workflows as workflows_mod

    api = _FakeWorkflowControllerApi()
    monkeypatch.setattr(
        workflows_mod, "current_rwc_api_client", SimpleNamespace(api=api)
    )
    # Valid token, but no reana_roles claim. The list endpoint is role-optional
    # and delegates user scoping to the workflow-controller.
    token = _make_token(auth_env)
    response = client.get(
        "/api/workflows", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}
    assert api.calls[0]["user"] == "11111111-1111-1111-1111-111111111111"


def test_workflows_with_required_role_is_200(client, auth_env, monkeypatch):
    import reana_server.rest.workflows as workflows_mod

    api = _FakeWorkflowControllerApi()
    monkeypatch.setattr(
        workflows_mod, "current_rwc_api_client", SimpleNamespace(api=api)
    )
    token = _make_token(auth_env, reana_roles=["reana:user"])
    response = client.get(
        "/api/workflows", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_you_returns_contract_shape(client, auth_env):
    token = _make_token(auth_env, reana_roles=["reana:user"])
    response = client.get(
        "/api/you", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "jane.doe@example.org"
    assert body["roles"] == ["reana:user"]
    assert body["identity"] == {"issuer": ISSUER, "subject": "subject-1"}
    assert body["id"]
    # Must not leak tokens/secrets/groups.
    assert "access_token" not in body and "groups" not in body


def test_openid_configuration_proxy(client, monkeypatch):
    import reana_server.rest.auth as auth_mod

    document = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/auth",
        "token_endpoint": f"{ISSUER}/token",
    }
    monkeypatch.setattr(
        auth_mod, "get_openid_configuration", lambda: document
    )
    response = client.get("/api/.well-known/openid-configuration")
    assert response.status_code == 200
    body = response.json()
    assert body["issuer"] == ISSUER
    assert body["reana_cli_client_id"] == "reana-cli"


def test_openid_configuration_proxy_rewrites_backchannel_urls(client, monkeypatch):
    import reana_server.rest.auth as auth_mod

    issuer = "https://localhost:30443/keycloak/realms/reana"
    monkeypatch.setitem(REANA_AUTH, "issuer", issuer)
    document = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/protocol/openid-connect/auth",
        "device_authorization_endpoint": (
            "http://reana-keycloak:8080/keycloak/realms/reana/"
            "protocol/openid-connect/auth/device"
        ),
        "token_endpoint": (
            "http://reana-keycloak:8080/keycloak/realms/reana/"
            "protocol/openid-connect/token"
        ),
        "jwks_uri": (
            "http://reana-keycloak:8080/keycloak/realms/reana/"
            "protocol/openid-connect/certs"
        ),
    }
    monkeypatch.setattr(
        auth_mod, "get_openid_configuration", lambda: document
    )

    response = client.get("/api/.well-known/openid-configuration")

    assert response.status_code == 200
    body = response.json()
    assert body["issuer"] == issuer
    assert body["device_authorization_endpoint"] == (
        "https://localhost:30443/keycloak/realms/reana/"
        "protocol/openid-connect/auth/device"
    )
    assert body["token_endpoint"] == (
        "https://localhost:30443/keycloak/realms/reana/"
        "protocol/openid-connect/token"
    )
    assert body["jwks_uri"] == (
        "https://localhost:30443/keycloak/realms/reana/"
        "protocol/openid-connect/certs"
    )


class _FakeBackend:
    provider = "keycloak"
    supports_search = True

    def __init__(self, refs=None, error=None):
        self._refs = refs or []
        self._error = error

    def search_groups(self, query, limit=20):
        if self._error:
            raise self._error
        return self._refs


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_groups_search_min_length(client, auth_env):
    token = _make_token(auth_env, reana_roles=["reana:user"])
    response = client.get("/api/groups/search?query=ab", headers=_bearer(token))
    assert response.status_code == 422


def test_groups_search_returns_items(client, auth_env, monkeypatch):
    from reana_server.groups.base import GroupRef
    import reana_server.rest.groups as groups_mod

    backend = _FakeBackend(
        refs=[
            GroupRef(
                "keycloak",
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "atlas",
                "/local/atlas",
            )
        ]
    )
    monkeypatch.setattr(
        groups_mod, "get_group_backends", lambda: {"keycloak": backend}
    )
    token = _make_token(auth_env, reana_roles=["reana:user"])
    response = client.get(
        "/api/groups/search?query=atl", headers=_bearer(token)
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item == {
        "provider": "keycloak",
        "external_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "display_name": "atlas",
        "path": "/local/atlas",
    }


def test_groups_search_backend_error_is_503(client, auth_env, monkeypatch):
    from reana_server.groups.base import GroupBackendError
    import reana_server.rest.groups as groups_mod

    backend = _FakeBackend(error=GroupBackendError("backend down"))
    monkeypatch.setattr(
        groups_mod, "get_group_backends", lambda: {"keycloak": backend}
    )
    token = _make_token(auth_env, reana_roles=["reana:user"])
    response = client.get(
        "/api/groups/search?query=atl", headers=_bearer(token)
    )
    assert response.status_code == 503


def test_groups_search_unknown_provider_is_404(client, auth_env, monkeypatch):
    import reana_server.rest.groups as groups_mod

    monkeypatch.setattr(groups_mod, "get_group_backend", lambda provider: None)
    token = _make_token(auth_env, reana_roles=["reana:user"])
    response = client.get(
        "/api/groups/search?query=atl&provider=nope", headers=_bearer(token)
    )
    assert response.status_code == 404
