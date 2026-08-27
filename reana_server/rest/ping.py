# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2020, 2021, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server Ping-functionality Flask-Blueprint."""

import logging

import redis
from flask import Blueprint, jsonify

from reana_server import __version__
from reana_server.auth.config import get_auth_config
from reana_server.auth.discovery import discovery_is_unavailable
from reana_server.auth.sessions import get_redis
from reana_server.auth.tokens import jwks_is_unavailable
from reana_server.config import REANA_API_CAPABILITIES

blueprint = Blueprint("ping", __name__)


@blueprint.route("/ping", methods=["GET"])
def ping():  # noqa
    r"""Endpoint to ping the server. Responds with a pong.
    ---
    get:
      summary: Ping the server (healthcheck)
      operationId: ping
      security: []
      description: >-
        Ping the server.


        This endpoint is deliberately unauthenticated: it also carries the
        protocol bootstrap signal that a client needs *before* it authenticates
        or builds a request body. ``api_capabilities`` advertises the
        client-facing protocols the server implements; a released server omits
        the field, which identifies it as legacy.
      produces:
       - application/json
      responses:
        200:
          description: >-
            Ping succeeded. Service is running and accessible.
          schema:
            type: object
            properties:
              message:
                type: string
              status:
                type: string
              reana_server_version:
                type: string
              api_capabilities:
                description: >-
                  Client-facing protocols implemented by this server. Absent on
                  released servers that predate protocol negotiation.
                type: array
                items:
                  type: string
          examples:
            application/json:
              message: OK
              status: 200
              reana_server_version: 0.95.0a6
              api_capabilities: ["workflow-specification-bundles-v1"]
    """

    return (
        jsonify(
            message="OK",
            status="200",
            reana_server_version=__version__,
            api_capabilities=REANA_API_CAPABILITIES,
        ),
        200,
    )


def _redis_check():
    """Return whether the BFF session store is reachable, or ``None`` if n/a.

    A live ``PING`` (bounded by the configured connect/socket timeouts) is
    cheap enough to run on every health check; unlike the issuer checks
    below, it needs no cached-state indirection.
    """
    if not get_auth_config().get("bff_enabled"):
        return None
    try:
        get_redis().ping()
        return True
    except (redis.RedisError, ValueError) as error:
        # get_redis() -> redis.Redis.from_url(...) raises ValueError (not
        # redis.RedisError) on a malformed REANA_AUTH_REDIS_URL, e.g. a bad
        # scheme. Without catching it here too, a misconfigured URL crashes
        # this unauthenticated endpoint with an uncontrolled 500 instead of
        # a controlled degraded-but-200 response.
        logging.warning("Health check: BFF session store is unreachable: %s", error)
        return False


def _issuer_check():
    """Return whether the OIDC issuer has usable cached material, or ``None``.

    Reads existing discovery/JWKS cache state only -- this must never
    trigger a network call to the issuer itself, both to keep a frequently
    polled health check cheap and to avoid a broken issuer turning
    Kubernetes' own probe traffic into additional load against it. A cache
    that has simply never been touched yet (fresh boot, no auth traffic) is
    not reported unhealthy: only a cache with no usable material *and* a
    known-failed most recent refresh is.
    """
    if not get_auth_config().get("issuer"):
        return None
    return not (discovery_is_unavailable() or jwks_is_unavailable())


@blueprint.route("/health", methods=["GET"])
def health():  # noqa
    r"""Endpoint reporting whether REANA Server's dependencies are usable.
    ---
    get:
      summary: Report health of REANA Server's auth-adjacent dependencies.
      operationId: health
      security: []
      description: >-
        Unlike /api/ping (process liveness only), this reports whether the
        BFF session store (Redis) and the configured OIDC issuer currently
        have usable cached material, so that external monitoring pointed
        here can detect an outage that /api/ping cannot see. Each check is
        skipped (omitted from the response and not counted against health)
        when the corresponding feature is not configured for this
        deployment. Never performs a live network call to the issuer; only
        inspects already-cached state, so this is cheap enough to poll
        frequently and cannot itself add load to a struggling issuer.


        Only the ``redis`` check (the BFF session store) can make this
        endpoint's own status code report unhealthy. This endpoint is for
        dependency monitoring, not Kubernetes readiness; deployments should
        probe /api/ping so a Redis outage does not withdraw the otherwise
        usable whole API. ``issuer`` is reported in the body for
        observability/alerting only and never affects the status code:
        with several worker processes sharing one pod, coupling readiness to
        issuer reachability could make an IdP outage take the whole pod out
        of rotation and keep it there even after the IdP recovers, because
        losing readiness also stops the very traffic that would refresh the
        issuer's discovery/JWKS caches. REANA Server keeps serving
        /api/ping, /api/info, /api/config, and the friendly 503 auth-error
        response regardless of issuer cache health.
      produces:
       - application/json
      responses:
        200:
          description: >-
            The BFF session store (when configured) is healthy. The issuer
            check may still be false; that is informational only.
          schema:
            type: object
            properties:
              status:
                type: string
              checks:
                type: object
          examples:
            application/json:
              status: ok
              checks:
                redis: true
                issuer: true
        503:
          description: >-
            The BFF session store (when configured) is unreachable.
          schema:
            type: object
            properties:
              status:
                type: string
              checks:
                type: object
    """
    all_checks = {"redis": _redis_check(), "issuer": _issuer_check()}
    # A None result means the corresponding feature is not configured for
    # this deployment (see _redis_check/_issuer_check); the docstring and
    # response schema above document that as "omitted from the response",
    # so filter it out here to match, rather than serialising a
    # not-configured feature as an explicit null the client must special-case.
    checks = {name: value for name, value in all_checks.items() if value is not None}
    # Readiness is derived from `redis` only. `issuer` is intentionally
    # excluded: it is still reported in `checks` below for observability, but
    # must never affect this endpoint's own status code -- see the docstring
    # above (and PR789-41) for why coupling k8s readiness to external-IdP
    # reachability is actively harmful (an outage could make the pod
    # unready and keep it that way, since losing readiness stops the
    # traffic that would let the issuer caches recover). This is a
    # decoupling, not a background-refresh/self-healing mechanism.
    ready = checks.get("redis") is not False
    status_code = 200 if ready else 503
    return (
        jsonify(status="ok" if ready else "unavailable", checks=checks),
        status_code,
    )
