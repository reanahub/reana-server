# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""OIDC discovery document handling.

Endpoints (JWKS, userinfo, token, device authorization, ...) are taken from
explicit ``REANA_AUTH_*`` configuration when given, and otherwise resolved
lazily from the issuer's ``/.well-known/openid-configuration`` document,
which is cached in-process.
"""

import threading
import time

import requests

from reana_server.config import REANA_AUTH
from reana_server.auth.errors import AuthError

_DISCOVERY_TTL = 3600

_lock = threading.Lock()
_cache = {"doc": None, "fetched_at": 0.0}

# Mapping from REANA_AUTH configuration keys to discovery document fields.
_ENDPOINT_KEYS = {
    "jwks_url": "jwks_uri",
    "userinfo_url": "userinfo_endpoint",
    "authorization_url": "authorization_endpoint",
    "token_url": "token_endpoint",
    "device_authorization_url": "device_authorization_endpoint",
    "end_session_url": "end_session_endpoint",
}


def get_openid_configuration_url():
    """Return the configured or issuer-derived OIDC discovery URL."""
    if REANA_AUTH["openid_config_url"]:
        return REANA_AUTH["openid_config_url"]
    if REANA_AUTH["issuer"]:
        return (
            REANA_AUTH["issuer"].rstrip("/") + "/.well-known/openid-configuration"
        )
    raise AuthError(
        "OIDC issuer is not configured "
        "(set REANA_AUTH_ISSUER or REANA_AUTH_OPENID_CONFIG_URL)."
    )


def get_openid_configuration(force=False):
    """Return the issuer's OIDC discovery document (cached in-process)."""
    with _lock:
        fresh = time.monotonic() - _cache["fetched_at"] < _DISCOVERY_TTL
        if _cache["doc"] is not None and fresh and not force:
            return _cache["doc"]
        url = get_openid_configuration_url()
        try:
            response = requests.get(url, timeout=REANA_AUTH["http_timeout"])
            response.raise_for_status()
            doc = response.json()
        except (requests.RequestException, ValueError) as error:
            if _cache["doc"] is not None:
                # Keep serving the stale document rather than failing hard.
                return _cache["doc"]
            raise AuthError(
                f"Could not fetch OIDC discovery document from {url}: {error}"
            )
        _cache["doc"] = doc
        _cache["fetched_at"] = time.monotonic()
        return doc


def get_endpoint(name):
    """Return an issuer endpoint URL by REANA_AUTH key (e.g. ``jwks_url``).

    Explicit configuration wins; otherwise the discovery document is used.
    """
    explicit = REANA_AUTH.get(name)
    if explicit:
        return explicit
    discovery_field = _ENDPOINT_KEYS[name]
    endpoint = get_openid_configuration().get(discovery_field)
    if not endpoint:
        raise AuthError(
            f"Issuer's OIDC discovery document does not advertise "
            f"'{discovery_field}'."
        )
    return endpoint
