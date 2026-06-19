# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021, 2022, 2024, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Pytest configuration for REANA-Server."""

from __future__ import absolute_import, print_function

from datetime import datetime, timedelta
import os
import time
from unittest.mock import Mock, patch

import pytest
from authlib.jose import JsonWebKey
from authlib.jose import jwt as jose_jwt

from reana_db.models import (
    WorkspaceRetentionAuditLog,
    WorkspaceRetentionRule,
    WorkspaceRetentionRuleStatus,
)

import reana_server.auth.tokens as _tokens_module
from reana_server.config import REANA_AUTH
from reana_server.factory import create_app

TEST_ISSUER = "https://auth.example.org/realms/reana"
"""Issuer used by the test JWKS fixtures."""


@pytest.fixture(scope="module")
def base_app(tmp_shared_volume_path):
    """Flask application fixture."""
    config_mapping = {
        "AVAILABLE_WORKFLOW_ENGINES": "serial",
        "SERVER_NAME": "localhost:5000",
        "SECRET_KEY": "SECRET_KEY",
        "TESTING": True,
        "DEBUG": True,
        "RATELIMIT_ENABLED": False,
        "SHARED_VOLUME_PATH": tmp_shared_volume_path,
        "SQLALCHEMY_DATABASE_URI": os.getenv("REANA_SQLALCHEMY_DATABASE_URI"),
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
    }
    app_ = create_app(config_mapping=config_mapping)
    return app_


@pytest.fixture(scope="session")
def jwt_signing_key():
    """Key pair whose public part is served as the test issuer's JWKS."""
    return JsonWebKey.generate_key("EC", "P-256", is_private=True)


@pytest.fixture(autouse=True)
def jwt_issuer(monkeypatch, jwt_signing_key):
    """Point JWT validation at the test issuer and serve its JWKS."""
    monkeypatch.setitem(REANA_AUTH, "issuer", TEST_ISSUER)
    monkeypatch.setitem(REANA_AUTH, "audience", "reana")
    monkeypatch.setitem(REANA_AUTH, "jwks_url", f"{TEST_ISSUER}/jwks")
    monkeypatch.setitem(REANA_AUTH, "userinfo_url", f"{TEST_ISSUER}/userinfo")
    monkeypatch.setattr(_tokens_module, "_jwks_cache", None)
    jwks_response = Mock()
    jwks_response.raise_for_status = Mock()
    jwks_response.json = Mock(
        return_value={"keys": [jwt_signing_key.as_dict(private=False)]}
    )
    with patch.object(
        _tokens_module.requests, "get", return_value=jwks_response
    ):
        yield


@pytest.fixture()
def make_token(jwt_signing_key):
    """Mint access tokens signed by the test issuer."""

    def _make_token(sub, roles=("reana:user",), **overrides):
        now = int(time.time())
        claims = {
            "iss": TEST_ISSUER,
            "aud": "reana",
            "sub": str(sub),
            "iat": now,
            "exp": now + 600,
            "reana_roles": list(roles),
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        header = {
            "alg": "ES256",
            "kid": jwt_signing_key.as_dict(private=False).get("kid"),
        }
        return jose_jwt.encode(header, claims, jwt_signing_key).decode()

    return _make_token


@pytest.fixture()
def auth_headers(make_token, session):
    """Authenticate a fixture user: link the IdP identity, mint a Bearer.

    Linking ``(iss, sub)`` on the user row makes just-in-time provisioning
    resolve the identity by lookup, so no userinfo mock is needed.
    """

    def _auth_headers(user, roles=("reana:user",)):
        if not user.idp_subject:
            user.idp_issuer = TEST_ISSUER
            user.idp_subject = str(user.id_)
            session.commit()
        token = make_token(user.idp_subject, roles=roles)
        return {"Authorization": f"Bearer {token}"}

    return _auth_headers


@pytest.fixture()
def workflow_with_retention_rules(sample_serial_workflow_in_db, session):
    workflow = sample_serial_workflow_in_db
    workflow.reana_specification = dict(workflow.reana_specification)
    workflow.reana_specification["inputs"] = {
        "files": ["input.txt", "to_be_deleted/input.txt"],
        "directories": ["inputs", "to_be_deleted/inputs"],
    }
    workflow.reana_specification["outputs"] = {
        "files": ["output.txt", "to_be_deleted/output.txt"],
        "directories": ["outputs", "to_be_deleted/outputs"],
    }
    current_time = datetime.now()

    def create_retention_rule(
        pattern, days, status=WorkspaceRetentionRuleStatus.active
    ):
        return WorkspaceRetentionRule(
            workflow_id=workflow.id_,
            workspace_files=pattern,
            retention_days=2 + days,
            status=status,
            apply_on=current_time + timedelta(days=days),
        )

    workflow.retention_rules = [
        create_retention_rule(
            "this_matches_nothing",
            days=-2,
            status=WorkspaceRetentionRuleStatus.pending,
        ),
        create_retention_rule("inputs", days=-1),
        create_retention_rule("**/*.txt", days=-1),
        create_retention_rule("to_be_deleted", days=-1),
        create_retention_rule("**/*", days=+1),
    ]
    session.add_all(workflow.retention_rules)
    session.add(workflow)
    session.commit()

    yield workflow

    session.query(WorkspaceRetentionAuditLog).delete()
    session.query(WorkspaceRetentionRule).delete()
    session.commit()
