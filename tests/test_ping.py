# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for the /api/ping and /api/health endpoints."""

import fakeredis
import time

from flask import url_for

import reana_server.auth.discovery as discovery_module
import reana_server.auth.sessions as sessions_module
import reana_server.auth.tokens as tokens_module
import reana_server.rest.ping as ping_module
from reana_server import __version__
from reana_server.config import WORKFLOW_SPECIFICATION_BUNDLES_CAPABILITY


def test_ping_advertises_protocol_capabilities(base_app):
    """Ping stays unauthenticated and carries the protocol bootstrap signal."""
    with base_app.app_context(), base_app.test_client() as client:
        res = client.get(url_for("ping.ping"))

        assert res.status_code == 200
        payload = res.json
        # Released clients parse ``status`` as a string; keep it one.
        assert payload["message"] == "OK"
        assert payload["status"] == "200"
        assert payload["reana_server_version"] == __version__
        assert WORKFLOW_SPECIFICATION_BUNDLES_CAPABILITY in payload["api_capabilities"]


def _reset_auth_caches(base_app):
    base_app.extensions.pop(discovery_module._DISCOVERY_EXTENSION, None)
    base_app.extensions.pop(tokens_module._JWKS_EXTENSION, None)


def test_health_ok_when_never_touched_and_redis_reachable(base_app):
    """A freshly booted cache (no traffic yet) reports healthy, not unknown."""
    _reset_auth_caches(base_app)
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    base_app.extensions[sessions_module._REDIS_EXTENSION] = fake_redis

    with base_app.test_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "checks": {"redis": True, "issuer": True},
    }


def test_health_reports_redis_unavailable(base_app, monkeypatch):
    """A Redis connection failure fails the health check, not just a log line."""
    _reset_auth_caches(base_app)

    class _BrokenRedis:
        def ping(self):
            raise sessions_module.redis.ConnectionError("connection refused")

    monkeypatch.setattr(ping_module, "get_redis", lambda: _BrokenRedis())

    with base_app.test_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "unavailable"
    assert body["checks"]["redis"] is False


def test_health_skips_redis_check_when_bff_disabled(base_app, monkeypatch):
    """A deployment without BFF/browser login has nothing to check there.

    A ``None`` result is omitted from the response entirely (per the
    endpoint's docstring/schema), not serialised as an explicit null the
    client would have to special-case.
    """
    _reset_auth_caches(base_app)
    monkeypatch.setitem(base_app.config["REANA_AUTH"], "bff_enabled", False)

    with base_app.test_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert "redis" not in response.get_json()["checks"]


def test_health_skips_issuer_check_when_no_issuer_configured(base_app, monkeypatch):
    """A deployment with no issuer configured has nothing to check there.

    A ``None`` result is omitted from the response entirely (per the
    endpoint's docstring/schema), not serialised as an explicit null the
    client would have to special-case.
    """
    _reset_auth_caches(base_app)
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    base_app.extensions[sessions_module._REDIS_EXTENSION] = fake_redis
    monkeypatch.setitem(base_app.config["REANA_AUTH"], "issuer", "")

    with base_app.test_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert "issuer" not in response.get_json()["checks"]


def test_health_reports_malformed_redis_url_without_500(base_app, monkeypatch):
    """A malformed REANA_AUTH_REDIS_URL degrades the check, not the endpoint.

    Regression test: ``redis.Redis.from_url(...)`` (called from
    ``get_redis()``) raises ``ValueError``, not ``redis.RedisError``, on a
    malformed URL such as a bad scheme. ``_redis_check`` previously only
    caught ``redis.RedisError``, so a misconfigured ``REANA_AUTH_REDIS_URL``
    crashed this unauthenticated endpoint with an uncontrolled 500 instead of
    a controlled degraded-but-200-or-503 response. No real Redis server is
    used or required here.
    """
    _reset_auth_caches(base_app)
    base_app.extensions.pop(sessions_module._REDIS_EXTENSION, None)
    monkeypatch.setitem(
        base_app.config["REANA_AUTH"], "redis_url", "not-a-valid-redis-url"
    )

    def _broken_from_url(*args, **kwargs):
        raise ValueError("Redis URL must specify one of the following schemes...")

    monkeypatch.setattr(sessions_module.redis.Redis, "from_url", _broken_from_url)

    with base_app.test_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "unavailable"
    assert body["checks"]["redis"] is False


def test_health_reports_issuer_unavailable_but_stays_ready(base_app):
    """A failed issuer refresh with nothing cached is reported, not readiness-gating.

    Regression test for PR789-41: readiness (this endpoint's own status code,
    what a Kubernetes readiness probe would key on) must be decoupled from
    external-IdP reachability. With multiple worker processes, coupling them
    means an IdP outage can take the whole pod out of rotation and keep it
    there even after the IdP recovers, because losing readiness also stops
    the very traffic that would refresh the discovery/JWKS caches. The
    issuer being fully unreachable with nothing cached must therefore still
    surface in the response body (for monitoring/alerting) but must NOT flip
    the endpoint's status code away from ready, unlike a Redis/BFF-session-
    store outage (see test_health_reports_redis_unavailable).
    """
    _reset_auth_caches(base_app)
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    base_app.extensions[sessions_module._REDIS_EXTENSION] = fake_redis
    with base_app.app_context():
        discovery_state = discovery_module._get_discovery_state()
        discovery_state["failed_at"] = time.monotonic()
        jwks_cache = tokens_module._get_jwks_cache()
        jwks_cache._refresh_failed_at = time.monotonic()

    with base_app.test_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["checks"]["issuer"] is False
    assert body["checks"]["redis"] is True


def test_health_ok_when_serving_stale_cached_material(base_app):
    """A stale-but-present cache is a soft degradation, not an outage.

    The single-flight refresh's whole point is to keep serving a usable
    stale document/key set through a transient issuer failure; the health
    check must not contradict that resilience by reporting healthy traffic
    as down.
    """
    _reset_auth_caches(base_app)
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    base_app.extensions[sessions_module._REDIS_EXTENSION] = fake_redis
    with base_app.app_context():
        discovery_state = discovery_module._get_discovery_state()
        discovery_state["doc"] = {"issuer": "https://stale.example.org"}
        # Within the bounded stale-grace window, same reasoning as the JWKS
        # cache below -- this test is about a cache that is
        # stale-but-still-usable, not one that has exceeded its grace period.
        discovery_state["fetched_at"] = time.monotonic()
        discovery_state["failed_at"] = time.monotonic()
        jwks_cache = tokens_module._get_jwks_cache()
        jwks_cache._key_set = object()
        # Within the bounded stale-grace window (ttl + stale_grace) -- this
        # test is specifically about a cache that is stale-but-still-usable,
        # not one that has exceeded its grace period.
        jwks_cache._fetched_at = time.monotonic()
        jwks_cache._refresh_failed_at = time.monotonic()

    with base_app.test_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.get_json()["checks"]["issuer"] is True


def test_health_reports_remembered_permanent_issuer_error(base_app):
    """Cached material must not mask a known permanent issuer defect."""
    _reset_auth_caches(base_app)
    base_app.extensions[sessions_module._REDIS_EXTENSION] = fakeredis.FakeRedis(
        decode_responses=True
    )
    with base_app.app_context():
        discovery_state = discovery_module._get_discovery_state()
        discovery_state["doc"] = {"issuer": "https://issuer.example.org"}
        discovery_state["fetched_at"] = time.monotonic()
        discovery_state["permanent_error"] = "invalid discovery document"

    with base_app.test_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.get_json()["checks"]["issuer"] is False
