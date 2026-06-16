# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for JWT validation and JIT provisioning."""

import time
from unittest.mock import Mock, patch

import pytest
from authlib.jose import JsonWebKey
from authlib.jose import jwt as jose_jwt

import reana_server.auth.tokens as tokens_module
from reana_server.auth.errors import (
    InvalidTokenError,
    MissingRoleError,
    ProvisioningError,
)
from reana_server.auth.provision import get_or_provision_user
from reana_server.auth.tokens import require_role, validate_access_token
from reana_server.config import REANA_AUTH

ISSUER = "https://auth.example.org/realms/reana"


def _generate_key():
    return JsonWebKey.generate_key("RSA", 2048, is_private=True)


def _jwks_response(*keys):
    response = Mock()
    response.raise_for_status = Mock()
    response.json = Mock(
        return_value={"keys": [k.as_dict(private=False) for k in keys]}
    )
    return response


def _make_token(key, **claim_overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "reana",
        "sub": "subject-1",
        "iat": now,
        "exp": now + 600,
    }
    claims.update(claim_overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    header = {"alg": "RS256", "kid": key.as_dict(private=False).get("kid")}
    return jose_jwt.encode(header, claims, key).decode()


@pytest.fixture
def signing_key():
    """RSA signing key whose public part is served as the issuer's JWKS."""
    return _generate_key()


@pytest.fixture
def auth_config(monkeypatch, signing_key):
    """Point REANA_AUTH at a fake issuer and serve its JWKS from a mock."""
    monkeypatch.setitem(REANA_AUTH, "issuer", ISSUER)
    monkeypatch.setitem(REANA_AUTH, "audience", "reana")
    monkeypatch.setitem(REANA_AUTH, "jwks_url", f"{ISSUER}/jwks")
    monkeypatch.setitem(REANA_AUTH, "userinfo_url", f"{ISSUER}/userinfo")
    monkeypatch.setattr(tokens_module, "_jwks_cache", None)
    with patch.object(
        tokens_module.requests,
        "get",
        return_value=_jwks_response(signing_key),
    ) as mocked_get:
        yield mocked_get


class TestValidateAccessToken:
    """Stateless validation against the trusted issuer."""

    def test_valid_token(self, auth_config, signing_key):
        claims = validate_access_token(_make_token(signing_key))
        assert claims["sub"] == "subject-1"
        assert claims["iss"] == ISSUER

    def test_wrong_issuer(self, auth_config, signing_key):
        token = _make_token(signing_key, iss="https://evil.example.org")
        with pytest.raises(InvalidTokenError):
            validate_access_token(token)

    def test_wrong_audience(self, auth_config, signing_key):
        token = _make_token(signing_key, aud="not-reana")
        with pytest.raises(InvalidTokenError):
            validate_access_token(token)

    def test_expired_token(self, auth_config, signing_key):
        # One hour in the past, far beyond the 30 s leeway.
        token = _make_token(signing_key, exp=int(time.time()) - 3600)
        with pytest.raises(InvalidTokenError):
            validate_access_token(token)

    def test_missing_sub(self, auth_config, signing_key):
        token = _make_token(signing_key, sub=None)
        with pytest.raises(InvalidTokenError):
            validate_access_token(token)

    def test_wrong_signature(self, auth_config):
        other_key = _generate_key()
        with pytest.raises(InvalidTokenError):
            validate_access_token(_make_token(other_key))

    def test_key_rotation_triggers_jwks_refetch(
        self, auth_config, signing_key
    ):
        """A token signed with a rotated key validates after one refetch."""
        new_key = _generate_key()
        auth_config.side_effect = [
            _jwks_response(signing_key),
            _jwks_response(signing_key, new_key),
        ]
        auth_config.return_value = None
        claims = validate_access_token(_make_token(new_key))
        assert claims["sub"] == "subject-1"
        assert auth_config.call_count == 2

    def test_jwks_served_stale_on_issuer_outage(
        self, auth_config, signing_key
    ):
        """Cached keys keep validating tokens while the issuer is down."""
        validate_access_token(_make_token(signing_key))
        cache = tokens_module._get_jwks_cache()
        cache._fetched_at = 0.0  # expire the cache
        auth_config.side_effect = tokens_module.requests.RequestException(
            "issuer down"
        )
        claims = validate_access_token(_make_token(signing_key))
        assert claims["sub"] == "subject-1"

    def test_unconfigured_issuer_rejects(self, monkeypatch, signing_key):
        monkeypatch.setitem(REANA_AUTH, "issuer", "")
        with pytest.raises(InvalidTokenError):
            validate_access_token(_make_token(signing_key))


class TestRequireRole:
    """The reana:user role gate."""

    def test_role_in_claims(self, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "required_role", "reana:user")
        require_role({"reana_roles": ["reana:user"]})

    def test_role_in_userinfo_fallback(self, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "required_role", "reana:user")
        require_role({}, userinfo={"reana_roles": ["reana:user"]})

    def test_missing_role(self, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "required_role", "reana:user")
        with pytest.raises(MissingRoleError):
            require_role({"reana_roles": ["something-else"]}, userinfo={})

    def test_disabled_gate(self, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "required_role", "")
        require_role({}, userinfo={})


class TestJITProvisioning:
    """Just-in-time user provisioning from userinfo."""

    @pytest.fixture
    def userinfo(self):
        return {
            "sub": "subject-jit",
            "email": "jane.doe@example.org",
            "email_verified": True,
            "name": "Jane Doe",
            "preferred_username": "jdoe",
            "reana_roles": ["reana:user"],
        }

    @pytest.fixture
    def claims(self):
        return {"iss": ISSUER, "sub": "subject-jit"}

    @pytest.fixture
    def enable_email_linking(self, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "email_linking_enabled", True)
        monkeypatch.setitem(REANA_AUTH, "email_linking_issuer_allowlist", [])
        monkeypatch.setitem(REANA_AUTH, "email_linking_domain_allowlist", [])

    def test_creates_user_once(self, app, session, claims, userinfo):
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ) as mocked_userinfo:
            user = get_or_provision_user(claims, "token")
            assert user.email == "jane.doe@example.org"
            assert user.idp_issuer == ISSUER
            assert user.idp_subject == "subject-jit"
            assert user.full_name == "Jane Doe"
            assert user.username == "jdoe"
            # Second call resolves by (iss, sub) without userinfo.
            again = get_or_provision_user(claims, "token")
            assert again.id_ == user.id_
            assert mocked_userinfo.call_count == 1

    def test_links_existing_unlinked_user_by_verified_email(
        self, app, session, default_user, claims, userinfo, enable_email_linking
    ):
        userinfo["email"] = default_user.email
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            user = get_or_provision_user(claims, "token")
        assert user.id_ == default_user.id_
        assert user.idp_subject == "subject-jit"

    def test_refuses_link_when_linking_disabled(
        self, app, session, default_user, claims, userinfo
    ):
        # Linking is disabled by default: an existing-email account must fail
        # closed for administrator resolution, not silently link or duplicate.
        userinfo["email"] = default_user.email
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            with pytest.raises(ProvisioningError):
                get_or_provision_user(claims, "token")
        assert default_user.idp_subject is None

    def test_rejects_userinfo_sub_mismatch(
        self, app, session, claims, userinfo
    ):
        userinfo["sub"] = "a-different-subject"
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            with pytest.raises(ProvisioningError):
                get_or_provision_user(claims, "token")

    def test_refuses_link_without_verified_email(
        self, app, session, default_user, claims, userinfo, enable_email_linking
    ):
        userinfo["email"] = default_user.email
        userinfo["email_verified"] = False
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            with pytest.raises(ProvisioningError):
                get_or_provision_user(claims, "token")
        assert default_user.idp_subject is None

    def test_refuses_link_to_already_linked_email(
        self, app, session, default_user, claims, userinfo, enable_email_linking
    ):
        default_user.idp_issuer = ISSUER
        default_user.idp_subject = "someone-else"
        session.commit()
        userinfo["email"] = default_user.email
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            with pytest.raises(ProvisioningError):
                get_or_provision_user(claims, "token")

    def test_role_gate_blocks_before_any_write(
        self, app, session, monkeypatch, claims, userinfo
    ):
        from reana_db.models import User

        monkeypatch.setitem(REANA_AUTH, "required_role", "reana:user")
        userinfo["reana_roles"] = []
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            with pytest.raises(MissingRoleError):
                get_or_provision_user(claims, "token")
        assert (
            session.query(User)
            .filter_by(email="jane.doe@example.org")
            .one_or_none()
            is None
        )
