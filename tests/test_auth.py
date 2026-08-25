# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for JWT validation and JIT provisioning."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from authlib.jose import JsonWebKey
from authlib.jose import jwt as jose_jwt
from reana_db.database import Session
from sqlalchemy.exc import IntegrityError

import reana_server.auth.tokens as tokens_module
from reana_server.auth.sessions import decode_expired_token
from reana_server.auth.errors import (
    AuthError,
    InvalidTokenError,
    IssuerMisconfiguredError,
    IssuerKeyUnavailableError,
    IssuerUnavailableError,
    MissingRoleError,
    ProvisioningError,
)
from reana_server.auth.provision import get_or_provision_user
from reana_server.auth.tokens import require_role, validate_access_token

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


_DEFAULT_KID = object()


def _make_token(key, token_kid=_DEFAULT_KID, algorithm="RS256", **claim_overrides):
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
    header = {"alg": algorithm}
    if token_kid is _DEFAULT_KID:
        header["kid"] = key.as_dict(private=False).get("kid")
    elif token_kid is not None:
        header["kid"] = token_kid
    return jose_jwt.encode(header, claims, key).decode()


@pytest.fixture
def signing_key():
    """RSA signing key whose public part is served as the issuer's JWKS."""
    return _generate_key()


@pytest.fixture
def auth_config(base_app, monkeypatch, signing_key):
    """Point REANA_AUTH at a fake issuer and serve its JWKS from a mock."""
    auth = base_app.config["REANA_AUTH"]
    monkeypatch.setitem(auth, "issuer", ISSUER)
    monkeypatch.setitem(auth, "audience", ["reana"])
    monkeypatch.setitem(auth, "jwks_url", f"{ISSUER}/jwks")
    monkeypatch.setitem(auth, "userinfo_url", f"{ISSUER}/userinfo")
    base_app.extensions.pop(tokens_module._JWKS_EXTENSION, None)
    with patch.object(
        tokens_module.requests,
        "get",
        return_value=_jwks_response(signing_key),
    ) as mocked_get, base_app.app_context():
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

    def test_any_configured_audience_is_accepted(
        self, base_app, auth_config, signing_key, monkeypatch
    ):
        """Both web and CLI audiences remain valid when configured together."""
        monkeypatch.setitem(base_app.config["REANA_AUTH"], "audience", ["cli", "web"])
        assert validate_access_token(_make_token(signing_key, aud="web"))["sub"]
        for audience in ("other", None):
            with pytest.raises(InvalidTokenError):
                validate_access_token(_make_token(signing_key, aud=audience))

    def test_expired_cookie_uses_the_same_audience_set(
        self, base_app, auth_config, signing_key, monkeypatch
    ):
        """Refreshable cookies accept either configured access-token audience."""
        monkeypatch.setitem(base_app.config["REANA_AUTH"], "audience", ["cli", "web"])
        expired = int(time.time()) - 3600
        assert decode_expired_token(_make_token(signing_key, aud="cli", exp=expired))[
            "sub"
        ]
        for audience in ("other", None):
            with pytest.raises(InvalidTokenError):
                decode_expired_token(
                    _make_token(signing_key, aud=audience, exp=expired)
                )

    @pytest.mark.parametrize("payload", [{}, {"keys": []}, {"keys": "bad"}])
    def test_unusable_jwks_is_an_availability_error(
        self, auth_config, signing_key, payload
    ):
        """A broken issuer key set is not blamed on the user's browser session."""
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = payload
        auth_config.return_value = response
        with pytest.raises(IssuerKeyUnavailableError):
            validate_access_token(_make_token(signing_key))

    def test_unknown_rotated_key_during_outage_is_an_availability_error(
        self, auth_config, signing_key
    ):
        """A recoverable warm session survives a failed rotation lookup."""
        validate_access_token(_make_token(signing_key))
        auth_config.side_effect = tokens_module.requests.RequestException("issuer down")
        with pytest.raises(IssuerKeyUnavailableError):
            validate_access_token(_make_token(_generate_key()))

    def test_id_token_audience_is_rejected_as_an_access_token(
        self, auth_config, signing_key
    ):
        """A web-client ID token cannot be confused with an API access token."""
        token = _make_token(signing_key, aud="reana-server", nonce="nonce")
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

    def test_same_kid_wrong_signature_does_not_refresh(self, auth_config, signing_key):
        """A bad signature under a known key id stays entirely local."""
        validate_access_token(_make_token(signing_key))
        other_key = _generate_key()
        token = _make_token(
            other_key,
            token_kid=signing_key.as_dict(private=False).get("kid"),
        )
        with pytest.raises(InvalidTokenError):
            validate_access_token(token)
        assert auth_config.call_count == 1

    def test_missing_kid_does_not_refresh(self, auth_config, signing_key):
        """A kid-less token is checked against cached keys without a refetch."""
        validate_access_token(_make_token(signing_key))
        with pytest.raises(InvalidTokenError):
            validate_access_token(_make_token(_generate_key(), token_kid=None))
        assert auth_config.call_count == 1

    def test_disallowed_algorithm_does_not_fetch_keys(self, auth_config):
        """Algorithm confusion is rejected before JWKS lookup."""
        with pytest.raises(InvalidTokenError):
            validate_access_token("eyJhbGciOiJIUzI1NiJ9.e30.c2ln")
        auth_config.assert_not_called()

    def test_key_rotation_triggers_jwks_refetch(self, auth_config, signing_key):
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

    def test_unknown_kid_is_negatively_cached(self, auth_config, signing_key):
        """Repeated tokens with one absent key id cause at most one refresh."""
        validate_access_token(_make_token(signing_key))
        unknown_key = _generate_key()
        token = _make_token(unknown_key)
        with pytest.raises(InvalidTokenError):
            validate_access_token(token)
        calls_after_refresh = auth_config.call_count
        with pytest.raises(InvalidTokenError):
            validate_access_token(token)
        assert calls_after_refresh == 2
        assert auth_config.call_count == calls_after_refresh

    def test_expired_unknown_kids_are_pruned(self, auth_config, signing_key):
        """Expired negative-cache entries do not accumulate indefinitely."""
        validate_access_token(_make_token(signing_key))
        cache = tokens_module._get_jwks_cache()
        cache._unknown_kids["expired"] = time.monotonic() - 1

        validate_access_token(_make_token(signing_key))

        assert "expired" not in cache._unknown_kids

    def test_global_unknown_kid_backoff_does_not_grow_cache(
        self, auth_config, signing_key
    ):
        """Random key ids during global backoff are transient and not retained."""
        validate_access_token(_make_token(signing_key))
        cache = tokens_module._get_jwks_cache()
        cache.get_key_set_for_kid("first-unknown")

        for index in range(100):
            with pytest.raises(IssuerKeyUnavailableError):
                cache.get_key_set_for_kid(f"random-{index}")

        assert set(cache._unknown_kids) == {"first-unknown"}
        assert auth_config.call_count == 2

    def test_rotated_key_during_unknown_kid_backoff_recovers_after_backoff(
        self, auth_config, signing_key
    ):
        """An ambiguous rotation is temporary, then gets a guarded refresh."""
        validate_access_token(_make_token(signing_key))
        unknown_key = _generate_key()
        with pytest.raises(InvalidTokenError):
            validate_access_token(_make_token(unknown_key))

        rotated_key = _generate_key()
        rotated_token = _make_token(rotated_key)
        with pytest.raises(IssuerKeyUnavailableError):
            validate_access_token(rotated_token)
        assert auth_config.call_count == 2

        cache = tokens_module._get_jwks_cache()
        cache._last_unknown_kid_refresh -= (
            tokens_module._MIN_UNKNOWN_KID_REFRESH_INTERVAL + 1
        )
        auth_config.return_value = _jwks_response(signing_key, rotated_key)

        assert validate_access_token(rotated_token)["sub"] == "subject-1"
        assert auth_config.call_count == 3

    def test_unknown_kid_cache_is_bounded_during_issuer_outage(
        self, auth_config, signing_key
    ):
        """A key-id flood while the issuer is down cannot grow without bound."""
        validate_access_token(_make_token(signing_key))
        cache = tokens_module._get_jwks_cache()

        # Every iteration finds a stale cache that cannot be refreshed, which
        # is the outage path that previously recorded one entry per request.
        for index in range(tokens_module._MAX_UNKNOWN_KIDS * 4):
            cache._fetched_at = 0.0
            cache._last_unknown_kid_refresh = 0.0
            cache._refresh_failed_at = 0.0
            cache.get_key_set_for_kid(f"flood-{index}")

        assert len(cache._unknown_kids) <= tokens_module._MAX_UNKNOWN_KIDS

    def test_stale_jwks_unknown_kid_fetches_only_once(self, auth_config, signing_key):
        """A TTL refresh also serves as the unknown-key refresh attempt."""
        validate_access_token(_make_token(signing_key))
        cache = tokens_module._get_jwks_cache()
        cache._fetched_at = 0.0
        token = _make_token(_generate_key())

        with pytest.raises(InvalidTokenError):
            validate_access_token(token)
        assert auth_config.call_count == 2

        with pytest.raises(InvalidTokenError):
            validate_access_token(token)
        assert auth_config.call_count == 2

    def test_id_and_expired_token_validation_do_not_refresh_bad_same_kid(
        self, auth_config, signing_key
    ):
        """The BFF validation sites share the unknown-kid-only refresh rule."""
        validate_access_token(_make_token(signing_key))
        other_key = _generate_key()
        known_kid = signing_key.as_dict(private=False).get("kid")
        id_token = _make_token(
            other_key,
            token_kid=known_kid,
            aud="reana-server",
            nonce="expected",
        )
        expired_cookie = _make_token(
            other_key,
            token_kid=known_kid,
            exp=int(time.time()) - 3600,
        )
        with pytest.raises(InvalidTokenError):
            tokens_module.validate_id_token(id_token, nonce="expected")
        with pytest.raises(InvalidTokenError):
            decode_expired_token(expired_cookie)
        assert auth_config.call_count == 1

    def test_decode_expired_token_reports_issuer_outage_distinctly(
        self, base_app, signing_key
    ):
        """A JWKS-fetch failure during expired-cookie decode is not "invalid".

        Regression test for the same misclassification already fixed in
        JWKSCache._fetch/_refresh (test_discovery_failure_does_not_wedge_
        refresh above): decode_expired_token's own broad ``except
        Exception`` previously reclassified IssuerUnavailableError as
        InvalidTokenError, which decorators.py's _authenticate then
        promotes to _TerminalSessionError -- clearing cookies over a
        transient, potentially self-healing issuer outage rather than
        surfacing it as an availability problem.
        """
        with base_app.app_context(), patch.object(
            tokens_module, "get_endpoint", side_effect=AuthError("unavailable")
        ):
            with pytest.raises(IssuerUnavailableError):
                decode_expired_token(_make_token(signing_key))

    def test_jwks_served_stale_on_issuer_outage(self, auth_config, signing_key):
        """Cached keys keep validating tokens while the issuer is down."""
        validate_access_token(_make_token(signing_key))
        cache = tokens_module._get_jwks_cache()
        cache._fetched_at = 0.0  # expire the cache
        auth_config.side_effect = tokens_module.requests.RequestException("issuer down")
        claims = validate_access_token(_make_token(signing_key))
        assert claims["sub"] == "subject-1"

    def test_unconfigured_issuer_rejects(self, base_app, monkeypatch, signing_key):
        monkeypatch.setitem(base_app.config["REANA_AUTH"], "issuer", "")
        with base_app.app_context(), pytest.raises(InvalidTokenError):
            validate_access_token(_make_token(signing_key))

    def test_discovery_failure_does_not_wedge_refresh(self, base_app, signing_key):
        """A discovery error always releases refresh waiters and permits retry."""
        with base_app.app_context(), patch.object(
            tokens_module, "get_endpoint", side_effect=AuthError("unavailable")
        ):
            for _ in range(2):
                with pytest.raises(IssuerUnavailableError):
                    validate_access_token(_make_token(signing_key))
                cache = tokens_module._get_jwks_cache()
                assert cache._refresh_in_progress is False

    def test_misconfigured_jwks_endpoint_surfaces_with_warm_cache(
        self, auth_config, signing_key
    ):
        """A permanent JWKS misconfiguration surfaces, not a silent stale serve.

        Regression test: ``IssuerMisconfiguredError`` is-an ``AuthError`` and
        was previously caught by the same broad transport/decoding except
        clause as transient failures in ``JWKSCache._fetch``/``_refresh``.
        Once the cache held a key set, that misclassification made a
        permanent configuration defect (e.g. ``get_endpoint`` rejecting the
        configured/discovered JWKS URL) silently absorbed forever behind the
        "serve stale cached key set" fallback, instead of surfacing so an
        administrator can fix it.
        """
        validate_access_token(_make_token(signing_key))
        cache = tokens_module._get_jwks_cache()
        cache._fetched_at = 0.0  # force the TTL-expired refresh path
        with patch.object(
            tokens_module,
            "get_endpoint",
            side_effect=IssuerMisconfiguredError(
                "jwks endpoint outside trust boundary"
            ),
        ):
            with pytest.raises(IssuerMisconfiguredError):
                cache.get_key_set()
        assert cache._refresh_in_progress is False
        # Unlike a transient issuer outage, this failure family must not be
        # silently absorbed: the stale key set is still there for other
        # callers to keep validating with, but *this* refresh must have
        # surfaced the defect rather than quietly returning it.
        assert cache._key_set is not None

    def test_misconfigured_jwks_surfaces_with_warm_cache_under_concurrent_refresh(
        self, auth_config, signing_key, base_app
    ):
        """Concurrent variant: single-flight bookkeeping also isn't wedged.

        Mirrors ``test_concurrent_discovery_refresh_makes_one_issuer_call`` in
        ``tests/test_auth_discovery.py``: a second caller arriving while the
        first is mid-refresh must reuse the still-cached (stale) key set
        rather than duplicate the failing refresh, and the failing refresh
        itself must still release ``_refresh_in_progress`` for the next
        caller instead of wedging it.
        """
        validate_access_token(_make_token(signing_key))
        cache = tokens_module._get_jwks_cache()
        cache._fetched_at = 0.0  # force the TTL-expired refresh path
        original_key_set = cache._key_set

        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def slow_get_endpoint(*args, **kwargs):
            fetch_started.set()
            # Hold the "network" open so the second caller must coordinate
            # through the single-flight refresh instead of issuing its own.
            release_fetch.wait(timeout=5)
            raise IssuerMisconfiguredError("jwks endpoint outside trust boundary")

        results = {}

        def worker(name):
            with base_app.app_context():
                try:
                    results[name] = cache.get_key_set()
                except Exception as error:  # noqa: BLE001
                    results[name] = error

        with patch.object(tokens_module, "get_endpoint", side_effect=slow_get_endpoint):
            first = threading.Thread(target=worker, args=("first",))
            first.start()
            assert fetch_started.wait(timeout=5)
            second = threading.Thread(target=worker, args=("second",))
            second.start()
            # Let the second caller reach the in-flight fast path before the
            # first's fetch raises.
            time.sleep(0.1)
            release_fetch.set()
            first.join(timeout=5)
            second.join(timeout=5)

        assert isinstance(results["first"], IssuerMisconfiguredError)
        assert results["second"] is original_key_set
        assert cache._refresh_in_progress is False
        assert cache._refresh_failed_at > 0


class TestRequireRole:
    """The reana:user role gate."""

    def test_role_in_claims(self, base_app, monkeypatch):
        monkeypatch.setitem(
            base_app.config["REANA_AUTH"], "required_role", "reana:user"
        )
        with base_app.app_context():
            require_role({"reana_roles": ["reana:user"]})

    def test_role_in_userinfo_is_not_an_authorization_source(
        self, base_app, monkeypatch
    ):
        monkeypatch.setitem(
            base_app.config["REANA_AUTH"], "required_role", "reana:user"
        )
        with base_app.app_context(), pytest.raises(MissingRoleError):
            require_role({})

    def test_missing_role(self, base_app, monkeypatch):
        monkeypatch.setitem(
            base_app.config["REANA_AUTH"], "required_role", "reana:user"
        )
        with base_app.app_context(), pytest.raises(MissingRoleError):
            require_role({"reana_roles": ["something-else"]})

    def test_disabled_gate(self, base_app, monkeypatch):
        monkeypatch.setitem(base_app.config["REANA_AUTH"], "required_role", "")
        with base_app.app_context():
            require_role({})


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
        return {
            "iss": ISSUER,
            "sub": "subject-jit",
            "reana_roles": ["reana:user"],
        }

    def test_creates_user_once(self, app, session, claims, userinfo):
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ) as mocked_userinfo:
            user, is_new = get_or_provision_user(claims, "token")
            assert is_new is True
            assert user.email == "jane.doe@example.org"
            assert user.idp_issuer == ISSUER
            assert user.idp_subject == "subject-jit"
            assert user.full_name == "Jane Doe"
            assert user.username == "jdoe"
            # Second call resolves by (iss, sub) without userinfo.
            again, is_new = get_or_provision_user(claims, "token")
            assert again.id_ == user.id_
            assert is_new is False
            assert mocked_userinfo.call_count == 1

    def test_strips_control_characters_from_issuer_claims(
        self, app, session, claims, userinfo
    ):
        """A malicious/misconfigured issuer must not be able to inject via claims.

        ``email``, ``name`` and ``preferred_username`` come verbatim from the
        issuer's userinfo response; without sanitization a CR/LF or ANSI
        escape sequence embedded there would reach application logs (and, via
        ``reana-admin export-users``, a CSV writer) unescaped.
        """
        userinfo["name"] = "Jane\r\nFAKE LOG LINE Doe"
        userinfo["preferred_username"] = "jdoe\x1b[31m"
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            user, _is_new = get_or_provision_user(claims, "token")
        assert "\r" not in user.full_name
        assert "\n" not in user.full_name
        assert "\x1b" not in user.username

    def test_refuses_oversized_email(self, app, session, claims, userinfo):
        """An oversized email is rejected, not left to crash as a DataError."""
        userinfo["email"] = "a" * 250 + "@example.org"
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            with pytest.raises(ProvisioningError, match="maximum allowed length"):
                get_or_provision_user(claims, "token")

    def test_truncates_oversized_display_name(self, app, session, claims, userinfo):
        """Presentation-only fields are truncated, not rejected outright."""
        userinfo["name"] = "A" * 300
        userinfo["preferred_username"] = "B" * 300
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            user, _is_new = get_or_provision_user(claims, "token")
        assert len(user.full_name) == 255
        assert len(user.username) == 255

    def test_refuses_oversized_subject_claim(self, app, session, claims, userinfo):
        """An oversized token subject is rejected before any I/O."""
        claims["sub"] = "s" * 256
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ) as fetch_userinfo:
            with pytest.raises(ProvisioningError, match="maximum allowed length"):
                get_or_provision_user(claims, "token")
        fetch_userinfo.assert_not_called()

    def test_refuses_oversized_issuer_claim(self, app, session, claims, userinfo):
        """An oversized token issuer is rejected before any I/O."""
        claims["iss"] = "https://" + "a" * 256
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ) as fetch_userinfo:
            with pytest.raises(ProvisioningError, match="maximum allowed length"):
                get_or_provision_user(claims, "token")
        fetch_userinfo.assert_not_called()

    def test_links_existing_unlinked_user_by_verified_email(
        self, app, session, monkeypatch, default_user, claims, userinfo
    ):
        monkeypatch.setitem(app.config["REANA_AUTH"], "email_linking_enabled", True)
        userinfo["email"] = default_user.email
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            user, _is_new = get_or_provision_user(claims, "token")
        assert user.id_ == default_user.id_
        assert user.idp_subject == "subject-jit"

    def test_email_linking_is_disabled_by_default(
        self, app, session, monkeypatch, default_user, claims, userinfo
    ):
        monkeypatch.setitem(app.config["REANA_AUTH"], "email_linking_enabled", False)
        userinfo["email"] = default_user.email
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            with pytest.raises(ProvisioningError):
                get_or_provision_user(claims, "token")
        assert default_user.idp_subject is None

    def test_refuses_link_without_verified_email(
        self, app, session, monkeypatch, default_user, claims, userinfo
    ):
        monkeypatch.setitem(app.config["REANA_AUTH"], "email_linking_enabled", True)
        userinfo["email"] = default_user.email
        userinfo["email_verified"] = False
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            with pytest.raises(ProvisioningError):
                get_or_provision_user(claims, "token")
        assert default_user.idp_subject is None

    def test_links_existing_user_via_assume_verified_issuer(
        self, app, session, monkeypatch, default_user, claims, userinfo
    ):
        """An issuer that never emits ``email_verified`` can still be trusted.

        Some institutional issuers (e.g. CERN Keycloak) never emit the
        standard OIDC ``email_verified`` claim at all, even though their
        email is verified out-of-band. An administrator can explicitly
        attest to that for one issuer via ``email_linking_assume_verified_issuers``
        without weakening the check for any other issuer.
        """
        monkeypatch.setitem(app.config["REANA_AUTH"], "email_linking_enabled", True)
        monkeypatch.setitem(
            app.config["REANA_AUTH"],
            "email_linking_assume_verified_issuers",
            [ISSUER],
        )
        userinfo["email"] = default_user.email
        del userinfo["email_verified"]
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            user, _is_new = get_or_provision_user(claims, "token")
        assert user.id_ == default_user.id_
        assert user.idp_subject == "subject-jit"

    def test_refuses_link_for_unassumed_issuer_without_verified_email(
        self, app, session, monkeypatch, default_user, claims, userinfo
    ):
        """The escape hatch is per-issuer, not global."""
        monkeypatch.setitem(app.config["REANA_AUTH"], "email_linking_enabled", True)
        monkeypatch.setitem(
            app.config["REANA_AUTH"],
            "email_linking_assume_verified_issuers",
            ["https://a-different-issuer.example.org/realms/reana"],
        )
        userinfo["email"] = default_user.email
        del userinfo["email_verified"]
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ):
            with pytest.raises(ProvisioningError):
                get_or_provision_user(claims, "token")
        assert default_user.idp_subject is None

    def test_refuses_link_to_already_linked_email(
        self, app, session, default_user, claims, userinfo
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

        monkeypatch.setitem(app.config["REANA_AUTH"], "required_role", "reana:user")
        claims["reana_roles"] = []
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ) as fetch_userinfo:
            with pytest.raises(MissingRoleError):
                get_or_provision_user(claims, "token")
        fetch_userinfo.assert_not_called()
        assert (
            session.query(User).filter_by(email="jane.doe@example.org").one_or_none()
            is None
        )

    def test_userinfo_only_role_is_rejected_before_remote_io(
        self, app, session, claims, userinfo
    ):
        """A profile claim cannot substitute for the access-token entitlement."""
        claims["reana_roles"] = []
        userinfo["reana_roles"] = ["reana:user"]
        with patch(
            "reana_server.auth.provision.fetch_userinfo",
            return_value=userinfo,
        ) as fetch_userinfo:
            with pytest.raises(MissingRoleError):
                get_or_provision_user(claims, "token")
        fetch_userinfo.assert_not_called()

    def test_non_identity_constraint_race_reuses_identity_winner(
        self, app, session, claims, userinfo, default_user
    ):
        """A concurrent winner is reused regardless of surfaced constraint."""
        default_user.idp_issuer = ISSUER
        default_user.idp_subject = claims["sub"]
        session.commit()
        original_error = SimpleNamespace(
            diag=SimpleNamespace(constraint_name="user__email_key")
        )
        conflict = IntegrityError("INSERT", {}, original_error)
        with patch(
            "reana_server.auth.provision.get_user_by_idp_identity",
            side_effect=[None, default_user],
        ), patch(
            "reana_server.auth.provision.fetch_userinfo", return_value=userinfo
        ), patch.object(
            Session, "commit", side_effect=conflict
        ):
            user, is_new = get_or_provision_user(claims, "token")
        assert user.id_ == default_user.id_
        assert is_new is False

    def test_integrity_error_without_identity_winner_is_controlled(
        self, app, session, claims, userinfo
    ):
        """An unrelated uniqueness failure is exposed as a safe auth error."""
        original_error = SimpleNamespace(
            diag=SimpleNamespace(constraint_name="user__email_key")
        )
        conflict = IntegrityError("INSERT", {}, original_error)
        with patch(
            "reana_server.auth.provision.get_user_by_idp_identity",
            side_effect=[None, None],
        ) as get_by_identity, patch(
            "reana_server.auth.provision.fetch_userinfo", return_value=userinfo
        ), patch.object(
            Session, "commit", side_effect=conflict
        ):
            with pytest.raises(ProvisioningError) as raised:
                get_or_provision_user(claims, "token")
        assert "could not be linked safely" in str(raised.value)
        assert get_by_identity.call_count == 2
