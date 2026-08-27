# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for issuer-bound and transport-safe OIDC discovery."""

import threading
import time
from unittest.mock import Mock, patch

import pytest

import reana_server.auth.discovery as discovery
from reana_server.auth.errors import (
    AuthError,
    IssuerMisconfiguredError,
    IssuerUnavailableError,
)

ISSUER = "https://auth.example.org/realms/reana"


def _document(**overrides):
    """Return a minimal standards-compliant discovery document."""
    document = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "userinfo_endpoint": f"{ISSUER}/userinfo",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    document.update(overrides)
    return document


def _response(document):
    response = Mock()
    response.status_code = 200
    response.raise_for_status = Mock()
    response.json.return_value = document
    return response


def _enable_internal_backchannel(auth, monkeypatch):
    """Configure the exact bundled-Keycloak development backchannel."""
    base = "http://reana-keycloak:8080/keycloak/realms/reana"
    monkeypatch.setitem(auth, "backchannel_base_url", base)
    monkeypatch.setitem(auth, "backchannel_allow_http", True)
    return base


@pytest.fixture
def discovery_config(base_app, monkeypatch):
    """Configure discovery and clear this app's cached document."""
    auth = base_app.config["REANA_AUTH"]
    monkeypatch.setitem(auth, "issuer", ISSUER)
    monkeypatch.setitem(
        auth, "openid_config_url", f"{ISSUER}/.well-known/openid-configuration"
    )
    base_app.extensions.pop(discovery._DISCOVERY_EXTENSION, None)
    with base_app.app_context():
        yield auth


def test_valid_discovery_document_is_cached(discovery_config):
    """Valid issuer metadata is accepted and fetched once."""
    with patch.object(
        discovery.requests, "get", return_value=_response(_document())
    ) as mocked_get:
        first = discovery.get_openid_configuration()
        second = discovery.get_openid_configuration()
    assert first is second
    mocked_get.assert_called_once_with(
        f"{ISSUER}/.well-known/openid-configuration",
        timeout=10,
        allow_redirects=False,
        verify=True,
    )


def test_failed_stale_discovery_refresh_is_negatively_cached(discovery_config):
    """A stale usable document suppresses repeated synchronous issuer calls."""
    with patch.object(discovery.requests, "get", return_value=_response(_document())):
        cached = discovery.get_openid_configuration()
    state = discovery._get_discovery_state()
    # Age the cache past its TTL relative to the real clock. A literal ``0.0``
    # is not reliably stale, because ``time.monotonic()`` can be below the TTL
    # on a freshly booted CI runner, which would leave the document "fresh".
    state["fetched_at"] -= discovery._DISCOVERY_TTL + 1

    with patch.object(
        discovery.requests,
        "get",
        side_effect=discovery.requests.RequestException("issuer unavailable"),
    ) as mocked_get:
        first = discovery.get_openid_configuration()
        second = discovery.get_openid_configuration()

    assert first is cached
    assert second is cached
    assert state["failed_at"] > 0
    mocked_get.assert_called_once()


def test_concurrent_discovery_refresh_makes_one_issuer_call(discovery_config, base_app):
    """A slow issuer is fetched once while a second caller waits on the refresh."""
    fetch_started = threading.Event()
    release_fetch = threading.Event()

    def slow_get(*args, **kwargs):
        fetch_started.set()
        # Hold the "network" open so the second caller must coordinate through
        # the single-flight refresh instead of issuing its own request.
        release_fetch.wait(timeout=5)
        return _response(_document())

    results = {}

    def worker(name):
        with base_app.app_context():
            try:
                results[name] = discovery.get_openid_configuration()
            except Exception as error:  # noqa: BLE001
                results[name] = error

    with patch.object(discovery.requests, "get", side_effect=slow_get) as mocked_get:
        first = threading.Thread(target=worker, args=("first",))
        first.start()
        assert fetch_started.wait(timeout=5)
        second = threading.Thread(target=worker, args=("second",))
        second.start()
        # Let the second caller reach the in-flight wait before the fetch ends.
        time.sleep(0.1)
        release_fetch.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert mocked_get.call_count == 1
    cached = discovery.get_openid_configuration()
    assert results["first"] is cached
    assert results["second"] is cached


def test_unexpected_refresh_exception_does_not_wedge_single_flight_state(
    discovery_config,
):
    """An unexpected exception still clears refresh state for the next caller.

    Regression test for a bug where only the expected failure family
    (``requests.RequestException``, ``ValueError``, ``AuthError``) cleared
    ``refresh_in_progress``. Any other exception -- e.g. a genuine bug
    surfacing as ``RuntimeError`` -- left it set forever, since the chart
    disables time-based worker recycling (``max_worker_lifetime: 0``).
    """
    with patch.object(discovery.requests, "get", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            discovery.get_openid_configuration()

    state = discovery._get_discovery_state()
    assert state["refresh_in_progress"] is False
    # Clear the negative-failure cache (a real caller past the short TTL) so
    # the assertion below proves single-flight state itself was cleaned up,
    # not merely that the by-design short-lived failure backoff expired.
    state["failed_at"] -= discovery._DISCOVERY_FAILURE_TTL + 1

    with patch.object(
        discovery.requests, "get", return_value=_response(_document())
    ) as mocked_get:
        doc = discovery.get_openid_configuration()

    assert doc == _document()
    mocked_get.assert_called_once()


def test_misconfigured_issuer_surfaces_with_warm_cache_under_concurrent_refresh(
    discovery_config, base_app
):
    """A permanent configuration defect surfaces, not a silent stale serve.

    Regression test: ``IssuerMisconfiguredError`` is-an ``AuthError`` and was
    previously caught by the same broad transport/decoding except clause as
    ``IssuerUnavailableError`` in ``_fetch_discovery_document``. With a
    stale-but-present cached document, that misclassification let a
    misconfigured issuer (e.g. a discovery document that stops matching the
    configured issuer) look like a transient outage: it was silently
    rewrapped as ``IssuerUnavailableError`` and either masqueraded as a
    502/503 or, once served from the warm-cache fallback, was absorbed
    entirely -- the stale document kept being served forever with no
    surfaced error at all. This concurrent-refresh variant, mirroring
    ``test_concurrent_discovery_refresh_makes_one_issuer_call``, additionally
    proves the single-flight bookkeeping (``refresh_in_progress``/
    ``failed_at``) is cleaned up correctly for this failure family too.
    """
    with patch.object(discovery.requests, "get", return_value=_response(_document())):
        cached = discovery.get_openid_configuration()
    state = discovery._get_discovery_state()
    state["fetched_at"] -= discovery._DISCOVERY_TTL + 1

    fetch_started = threading.Event()
    release_fetch = threading.Event()
    misconfigured_document = _document(issuer="https://attacker.example")

    def slow_get(*args, **kwargs):
        fetch_started.set()
        # Hold the "network" open so the second caller must coordinate
        # through the single-flight refresh instead of issuing its own call.
        release_fetch.wait(timeout=5)
        return _response(misconfigured_document)

    results = {}

    def worker(name):
        with base_app.app_context():
            try:
                results[name] = discovery.get_openid_configuration()
            except Exception as error:  # noqa: BLE001
                results[name] = error

    with patch.object(discovery.requests, "get", side_effect=slow_get):
        first = threading.Thread(target=worker, args=("first",))
        first.start()
        assert fetch_started.wait(timeout=5)
        second = threading.Thread(target=worker, args=("second",))
        second.start()
        # Let the second caller reach the in-flight fast path before the
        # first's fetch resolves.
        time.sleep(0.1)
        release_fetch.set()
        first.join(timeout=5)
        second.join(timeout=5)

    # The caller that actually performed the refresh sees the real,
    # permanent-configuration-defect error, not a stale document and not a
    # misleadingly transient IssuerUnavailableError.
    assert isinstance(results["first"], IssuerMisconfiguredError)
    # The second caller reused the still-cached (stale) document rather than
    # waiting on/duplicating the failing refresh -- the existing single-flight
    # fast path for a caller that arrives while a refresh is already running.
    assert results["second"] is cached

    # Refresh bookkeeping must not be wedged by this failure family either.
    assert state["refresh_in_progress"] is False
    assert state["failed_at"] > 0


def test_misconfigured_issuer_surfaces_to_a_later_sequential_caller(
    discovery_config, base_app
):
    """A permanent defect is remembered, not just discovered once.

    Regression test for the reopened half of PR789-48: the refreshing
    caller itself already sees ``IssuerMisconfiguredError`` correctly (see
    the concurrent-refresh variant above), but that only fixed the
    in-progress-refresh path. A *sequential* caller -- arriving after the
    failing refresh has already completed, during the failure backoff
    window -- only saw a bare timestamp (``state["failed_at"]``), not the
    reason, and fell into ``_serve_stale_or_raise`` returning the stale-but-
    present document instead of the permanent error.
    """
    with patch.object(discovery.requests, "get", return_value=_response(_document())):
        discovery.get_openid_configuration()
    state = discovery._get_discovery_state()
    state["fetched_at"] -= discovery._DISCOVERY_TTL + 1

    misconfigured_document = _document(issuer="https://attacker.example")
    with patch.object(
        discovery.requests, "get", return_value=_response(misconfigured_document)
    ):
        with pytest.raises(IssuerMisconfiguredError):
            discovery.get_openid_configuration()

    # The failing refresh has fully completed (no thread coordination
    # needed) and recorded a permanent-error classification.
    assert state["refresh_in_progress"] is False
    assert state["permanent_error"] is not None

    # A later, unrelated caller arriving within the failure backoff window
    # must also see the permanent error, not the still-cached (stale)
    # document from before the misconfiguration was detected.
    with pytest.raises(IssuerMisconfiguredError):
        discovery.get_openid_configuration()


def test_recovery_probe_does_not_bypass_remembered_permanent_error(
    discovery_config, base_app
):
    """A caller arriving during post-backoff recovery must fail closed."""
    with patch.object(discovery.requests, "get", return_value=_response(_document())):
        cached = discovery.get_openid_configuration()
    state = discovery._get_discovery_state()
    state["fetched_at"] -= discovery._DISCOVERY_TTL + 1
    state["failed_at"] -= discovery._DISCOVERY_FAILURE_TTL + 1
    state["permanent_error"] = "issuer remains misconfigured"
    state["refresh_in_progress"] = True

    with pytest.raises(IssuerMisconfiguredError) as first:
        discovery.get_openid_configuration()
    with pytest.raises(IssuerMisconfiguredError) as second:
        discovery.get_openid_configuration()

    assert first.value is not second.value
    assert state["doc"] is cached


def test_fresh_discovery_document_precedes_old_permanent_verdict(
    discovery_config, base_app
):
    """A normal-TTL hit remains usable while a recovery probe is running."""
    with patch.object(discovery.requests, "get", return_value=_response(_document())):
        cached = discovery.get_openid_configuration()
    state = discovery._get_discovery_state()
    state["permanent_error"] = "older failed refresh"
    state["refresh_in_progress"] = True

    assert discovery.get_openid_configuration() is cached


def test_transient_failure_after_permanent_one_is_not_masked(
    discovery_config, base_app
):
    """The most recent refresh attempt's classification always wins.

    A permanent misconfiguration must not stay "sticky" forever: once an
    operator fixes the issuer and a later refresh attempt instead hits an
    ordinary transient failure (network error), that attempt's outcome
    (stale-serve, or a transient error if nothing is cached) must apply --
    not a stale permanent-error classification from an earlier attempt.
    """
    with patch.object(discovery.requests, "get", return_value=_response(_document())):
        cached = discovery.get_openid_configuration()
    state = discovery._get_discovery_state()
    state["fetched_at"] -= discovery._DISCOVERY_TTL + 1

    misconfigured_document = _document(issuer="https://attacker.example")
    with patch.object(
        discovery.requests, "get", return_value=_response(misconfigured_document)
    ):
        with pytest.raises(IssuerMisconfiguredError):
            discovery.get_openid_configuration()
    assert state["permanent_error"] is not None

    # Clear the backoff window and let the next attempt hit a transient
    # network failure instead. ``fetched_at`` is untouched: the earlier
    # failed refresh never updated it, so the document is still exactly as
    # stale (and still within its grace window) as it was for that attempt.
    state["failed_at"] = 0.0
    with patch.object(
        discovery.requests, "get", side_effect=discovery.requests.ConnectionError("x")
    ):
        result = discovery.get_openid_configuration()

    assert result is cached
    assert state["permanent_error"] is None


def test_cold_discovery_failure_is_an_availability_error(discovery_config):
    """Issuer outages are distinct from invalid user credentials."""
    with patch.object(
        discovery.requests,
        "get",
        side_effect=discovery.requests.RequestException("issuer unavailable"),
    ):
        with pytest.raises(IssuerUnavailableError):
            discovery.get_openid_configuration()


def test_discovery_issuer_must_match_exactly(discovery_config):
    """A document cannot redirect credentials by claiming another issuer."""
    document = _document(issuer="https://attacker.example")
    with patch.object(discovery.requests, "get", return_value=_response(document)):
        with pytest.raises(AuthError, match="exactly match"):
            discovery.get_openid_configuration()


def test_discovery_redirect_is_rejected(discovery_config):
    """Sensitive issuer requests never follow or accept redirects."""
    response = _response(_document())
    response.status_code = 302
    response.headers = {"Location": "https://attacker.example/discovery"}
    with patch.object(discovery.requests, "get", return_value=response):
        with pytest.raises(AuthError, match="Could not fetch"):
            discovery.get_openid_configuration()


@pytest.mark.parametrize(
    "field,value",
    [
        ("jwks_uri", None),
        ("response_types_supported", []),
        ("subject_types_supported", "public"),
    ],
)
def test_discovery_rejects_missing_or_malformed_required_fields(
    discovery_config, field, value
):
    """Required URL and capability metadata must be present and typed."""
    document = _document()
    if value is None:
        document.pop(field)
    else:
        document[field] = value
    with patch.object(discovery.requests, "get", return_value=_response(document)):
        with pytest.raises(AuthError, match="required field"):
            discovery.get_openid_configuration()


@pytest.mark.parametrize(
    "field,value",
    [
        ("token_endpoint", "http://attacker.example/steal-code"),
        ("token_endpoint", "https://attacker.example/steal-code"),
        ("jwks_uri", "file:///tmp/jwks.json"),
        ("authorization_endpoint", "https://user:pass@auth.example.org/login"),
    ],
)
def test_discovery_rejects_unsafe_endpoint_urls(
    discovery_config, monkeypatch, field, value
):
    """Advertised endpoints must be absolute credential-free HTTPS URLs."""
    config_key = next(
        name
        for name, discovery_field in discovery._ENDPOINT_KEYS.items()
        if discovery_field == field
    )
    monkeypatch.setitem(discovery_config, config_key, "")
    with patch.object(
        discovery.requests,
        "get",
        return_value=_response(_document(**{field: value})),
    ):
        with pytest.raises(AuthError):
            discovery.get_openid_configuration()


def test_explicit_internal_http_discovery_backchannel_is_allowed(
    discovery_config, monkeypatch
):
    """The bundled Keycloak Service remains a deliberate HTTP backchannel."""
    backchannel_base = _enable_internal_backchannel(discovery_config, monkeypatch)
    monkeypatch.setitem(
        discovery_config,
        "openid_config_url",
        f"{backchannel_base}/.well-known/openid-configuration",
    )
    assert discovery.get_openid_configuration_url() == (
        f"{backchannel_base}/.well-known/openid-configuration"
    )


def test_external_http_discovery_url_is_rejected(discovery_config, monkeypatch):
    """Explicit backchannels do not permit cleartext external hosts."""
    monkeypatch.setitem(
        discovery_config,
        "openid_config_url",
        "http://attacker.example/.well-known/openid-configuration",
    )
    with pytest.raises(AuthError, match="HTTPS"):
        discovery.get_openid_configuration_url()


def test_endpoint_allows_http_beneath_explicit_backchannel(
    discovery_config, monkeypatch
):
    """A public issuer can deliberately use the bundled HTTP backchannel."""
    backchannel_base = _enable_internal_backchannel(discovery_config, monkeypatch)
    monkeypatch.setitem(discovery_config, "jwks_url", "")
    monkeypatch.setattr(
        discovery,
        "get_openid_configuration",
        lambda: {"jwks_uri": f"{backchannel_base}/protocol/openid-connect/certs"},
    )
    assert discovery.get_endpoint("jwks_url") == (
        f"{backchannel_base}/protocol/openid-connect/certs"
    )


def test_endpoint_rejects_external_http_with_internal_backchannel(
    discovery_config, monkeypatch
):
    """The HTTP opt-in does not authorize cleartext external endpoints."""
    _enable_internal_backchannel(discovery_config, monkeypatch)
    monkeypatch.setitem(discovery_config, "jwks_url", "")
    monkeypatch.setattr(
        discovery,
        "get_openid_configuration",
        lambda: {"jwks_uri": "http://attacker.example/jwks"},
    )
    with pytest.raises(AuthError, match="HTTPS"):
        discovery.get_endpoint("jwks_url")


@pytest.mark.parametrize(
    "url",
    [
        "http://reana-keycloak:8080/keycloak/realms/other/jwks",
        "http://reana-keycloak:8080/keycloak/realms/reana-evil/jwks",
        "http://reana-keycloak:8080/keycloak/realms/reana/%2e%2e/other/jwks",
    ],
)
def test_endpoint_cannot_escape_exact_backchannel_path(
    discovery_config, monkeypatch, url
):
    """Host equality alone cannot escape the configured realm path."""
    _enable_internal_backchannel(discovery_config, monkeypatch)
    monkeypatch.setitem(discovery_config, "jwks_url", "")
    monkeypatch.setattr(
        discovery,
        "get_openid_configuration",
        lambda: {"jwks_uri": url},
    )
    with pytest.raises(AuthError, match="exact configured backchannel|boundaries"):
        discovery.get_endpoint("jwks_url")


def test_discovery_accepts_internal_endpoints_only_beneath_backchannel(
    discovery_config, monkeypatch
):
    """Keycloak may advertise internal token/JWKS/UserInfo endpoints."""
    backchannel_base = _enable_internal_backchannel(discovery_config, monkeypatch)
    document = _document(
        token_endpoint=f"{backchannel_base}/protocol/openid-connect/token",
        jwks_uri=f"{backchannel_base}/protocol/openid-connect/certs",
        userinfo_endpoint=f"{backchannel_base}/protocol/openid-connect/userinfo",
    )
    with patch.object(discovery.requests, "get", return_value=_response(document)):
        validated = discovery.get_openid_configuration()

    assert validated["token_endpoint"].startswith(backchannel_base)


def test_frontchannel_endpoint_must_remain_public_https(discovery_config, monkeypatch):
    """An internal backchannel cannot turn browser redirects into HTTP."""
    backchannel_base = _enable_internal_backchannel(discovery_config, monkeypatch)
    document = _document(
        authorization_endpoint=(f"{backchannel_base}/protocol/openid-connect/auth")
    )
    with patch.object(discovery.requests, "get", return_value=_response(document)):
        with pytest.raises(AuthError, match="public issuer origin"):
            discovery.get_openid_configuration()


def test_explicit_cross_origin_https_endpoint_is_trusted(discovery_config, monkeypatch):
    """Operators can explicitly allow a legitimate cross-origin endpoint."""
    endpoint = "https://tokens.example.net/oauth/token"
    monkeypatch.setitem(discovery_config, "token_url", endpoint)
    with patch.object(
        discovery.requests,
        "get",
        return_value=_response(_document(token_endpoint=endpoint)),
    ):
        document = discovery.get_openid_configuration()
    assert document["token_endpoint"] == endpoint


def test_http_opt_in_requires_backchannel_base(discovery_config, monkeypatch):
    """The insecure transport switch cannot be enabled on its own."""
    monkeypatch.setitem(discovery_config, "backchannel_base_url", "")
    monkeypatch.setitem(discovery_config, "backchannel_allow_http", True)
    with pytest.raises(AuthError, match="cannot be enabled without"):
        discovery.get_openid_configuration_url()


def test_public_issuer_must_use_https(discovery_config, monkeypatch):
    """The stable token issuer is never downgraded by the backchannel setting."""
    monkeypatch.setitem(
        discovery_config,
        "issuer",
        "http://reana-keycloak:8080/keycloak/realms/reana",
    )
    with pytest.raises(AuthError, match="must use HTTPS"):
        discovery.validate_auth_configuration()


def test_jwks_stale_grace_cannot_be_negative(discovery_config, monkeypatch):
    """The bounded stale-key fallback cannot be configured as unbounded."""
    monkeypatch.setitem(discovery_config, "jwks_stale_grace", -1)

    with pytest.raises(AuthError, match="stale grace"):
        discovery.validate_auth_configuration()


def test_discovery_stale_grace_cannot_be_negative(discovery_config, monkeypatch):
    """The bounded stale-document fallback cannot be configured as unbounded."""
    monkeypatch.setitem(discovery_config, "discovery_stale_grace", -1)

    with pytest.raises(AuthError, match="stale grace"):
        discovery.validate_auth_configuration()


def test_discovery_stale_grace_has_a_hard_cutoff(discovery_config):
    """A discovery document is not served once it exceeds its stale grace.

    Mirrors JWKSCache's equivalent hard-cutoff test: an issuer that rotates
    an endpoint (e.g. jwks_uri, as part of decommissioning a compromised
    one) must not have REANA keep resolving the old document indefinitely
    just because refreshes keep failing.
    """
    with patch.object(discovery.requests, "get", return_value=_response(_document())):
        discovery.get_openid_configuration()
    state = discovery._get_discovery_state()
    stale_grace = discovery.get_auth_config()["discovery_stale_grace"]
    state["fetched_at"] -= discovery._DISCOVERY_TTL + stale_grace + 1

    with patch.object(
        discovery.requests,
        "get",
        side_effect=discovery.requests.RequestException("issuer unavailable"),
    ):
        with pytest.raises(
            discovery.IssuerUnavailableError, match="exceeded its stale grace"
        ):
            discovery.get_openid_configuration()
    assert discovery.discovery_is_unavailable() is True


def test_zero_discovery_stale_grace_disables_outage_fallback(
    discovery_config, monkeypatch
):
    """Zero grace fails closed as soon as the normal TTL elapses."""
    monkeypatch.setitem(discovery_config, "discovery_stale_grace", 0)
    with patch.object(discovery.requests, "get", return_value=_response(_document())):
        discovery.get_openid_configuration()
    state = discovery._get_discovery_state()
    state["fetched_at"] -= discovery._DISCOVERY_TTL + 1

    with patch.object(
        discovery.requests,
        "get",
        side_effect=discovery.requests.RequestException("issuer unavailable"),
    ):
        with pytest.raises(discovery.IssuerUnavailableError):
            discovery.get_openid_configuration()
