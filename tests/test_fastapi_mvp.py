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
exercises the framework integration — routing, the OAuth2 scheme, the
SecurityScopes role gate and the 401/403 mapping — without the heavy app
fixture.
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
    monkeypatch.setattr(deps, "get_or_provision_user", lambda claims, token: fake_user)
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
    # The role-gated route carries the reana:user scope requirement.
    assert schema["paths"]["/api/workflows"]["get"]["security"] == [
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


def test_workflows_without_required_role_is_403(client, auth_env):
    # Valid token, but no reana_roles claim -> role gate rejects with 403.
    token = _make_token(auth_env)
    response = client.get(
        "/api/workflows", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_workflows_with_required_role_is_200(client, auth_env):
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
    import reana_server.fastapi_rest.auth as auth_mod

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


class _FakeBackend:
    provider = "keycloak"

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
    import reana_server.fastapi_rest.groups as groups_mod

    backend = _FakeBackend(
        refs=[GroupRef("keycloak", "/local/atlas", "atlas", "/local/atlas")]
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
        "external_id": "/local/atlas",
        "display_name": "atlas",
        "path": "/local/atlas",
    }


def test_groups_search_backend_error_is_503(client, auth_env, monkeypatch):
    from reana_server.groups.base import GroupBackendError
    import reana_server.fastapi_rest.groups as groups_mod

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
    import reana_server.fastapi_rest.groups as groups_mod

    monkeypatch.setattr(groups_mod, "get_group_backend", lambda provider: None)
    token = _make_token(auth_env, reana_roles=["reana:user"])
    response = client.get(
        "/api/groups/search?query=atl&provider=nope", headers=_bearer(token)
    )
    assert response.status_code == 404
