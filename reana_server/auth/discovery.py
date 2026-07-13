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

import logging
import posixpath
import threading
import time
from ipaddress import ip_address
from urllib.parse import unquote, urlsplit

import requests
from flask import current_app

from reana_server.auth.config import (
    get_auth_config,
    get_issuer_request_kwargs,
    raise_for_issuer_status,
)
from reana_server.auth.errors import (
    AuthError,
    IssuerMisconfiguredError,
    IssuerUnavailableError,
)

_DISCOVERY_TTL = 3600
_DISCOVERY_FAILURE_TTL = 5

_DISCOVERY_EXTENSION = "reana_auth_discovery"

# Mapping from REANA_AUTH configuration keys to discovery document fields.
_ENDPOINT_KEYS = {
    "jwks_url": "jwks_uri",
    "userinfo_url": "userinfo_endpoint",
    "authorization_url": "authorization_endpoint",
    "token_url": "token_endpoint",
    "device_authorization_url": "device_authorization_endpoint",
    "end_session_url": "end_session_endpoint",
}
_FRONTCHANNEL_ENDPOINT_KEYS = {"authorization_url", "end_session_url"}

_REQUIRED_URL_FIELDS = (
    "authorization_endpoint",
    "token_endpoint",
    "jwks_uri",
    "userinfo_endpoint",
)
_REQUIRED_LIST_FIELDS = (
    "response_types_supported",
    "subject_types_supported",
    "id_token_signing_alg_values_supported",
)


def _is_internal_host(hostname):
    """Return whether ``hostname`` is a loopback/private cluster address."""
    if not hostname:
        return False
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(
        (".localhost", ".svc", ".cluster.local")
    ):
        return True
    if "." not in lowered:
        # Kubernetes service names are commonly single-label DNS names.
        return True
    try:
        address = ip_address(lowered)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _validate_url(value, label, allow_internal_http=False):
    """Validate an absolute credential-free HTTPS URL.

    Plain HTTP is accepted only for an explicitly configured internal
    backchannel (for example the bundled Keycloak Kubernetes Service).
    """
    if not isinstance(value, str) or not value:
        raise IssuerMisconfiguredError(f"OIDC {label} must be a non-empty URL.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as error:
        raise IssuerMisconfiguredError(f"OIDC {label} is not a valid URL: {error}")
    if (
        not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise IssuerMisconfiguredError(
            f"OIDC {label} must be an absolute URL without credentials or a fragment."
        )
    if parsed.scheme == "https":
        return value
    if parsed.scheme == "http" and allow_internal_http and _is_internal_host(hostname):
        return value
    raise IssuerMisconfiguredError(f"OIDC {label} must use HTTPS.")


def _validate_base_url(value, label, allow_internal_http=False):
    """Validate a URL used as an issuer or trusted endpoint base."""
    _validate_url(value, label, allow_internal_http=allow_internal_http)
    parsed = urlsplit(value)
    if parsed.query:
        raise IssuerMisconfiguredError(f"OIDC {label} must not contain a query string.")
    return value.rstrip("/")


def _normalise_path(path):
    """Return a comparison-safe URL path, decoding nested escapes."""
    decoded = path or "/"
    for _ in range(3):
        unquoted = unquote(decoded)
        if unquoted == decoded:
            break
        decoded = unquoted
    if "\\" in decoded or "\x00" in decoded:
        return None
    normalised = posixpath.normpath(decoded)
    if not normalised.startswith("/"):
        normalised = "/" + normalised
    return normalised


def _is_beneath_base(value, base):
    """Return whether ``value`` is on the exact origin and path below ``base``."""
    try:
        parsed = urlsplit(value)
        base_parsed = urlsplit(base)
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != base_parsed.scheme.lower()
        or parsed.netloc.lower() != base_parsed.netloc.lower()
    ):
        return False
    path = _normalise_path(parsed.path)
    base_path = _normalise_path(base_parsed.path)
    if path is None or base_path is None:
        return False
    base_path = base_path.rstrip("/") or "/"
    if base_path == "/":
        return path.startswith("/")
    return path == base_path or path.startswith(base_path + "/")


def _same_origin(value, base):
    """Return whether two validated URLs have the same scheme/host/port."""
    try:
        parsed = urlsplit(value)
        base_parsed = urlsplit(base)
        parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        base_port = base_parsed.port or (443 if base_parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == base_parsed.scheme.lower()
        and parsed.hostname.lower() == base_parsed.hostname.lower()
        and parsed_port == base_port
    )


def _get_backchannel_base():
    """Return the validated, independently configured OIDC backchannel base."""
    auth_config = get_auth_config()
    base = auth_config.get("backchannel_base_url", "")
    allow_http = auth_config.get("backchannel_allow_http", False)
    if not base:
        if allow_http:
            raise IssuerMisconfiguredError(
                "OIDC backchannel HTTP cannot be enabled without "
                "REANA_AUTH_BACKCHANNEL_BASE_URL."
            )
        return ""
    validated = _validate_base_url(
        base,
        "backchannel base URL",
        allow_internal_http=allow_http,
    )
    scheme = urlsplit(validated).scheme.lower()
    if allow_http and scheme != "http":
        raise IssuerMisconfiguredError(
            "OIDC backchannel HTTP is enabled but the configured base uses HTTPS."
        )
    return validated


def _validate_configured_endpoint(value, label):
    """Validate an operator-configured endpoint override."""
    backchannel_base = _get_backchannel_base()
    try:
        scheme = urlsplit(value).scheme.lower()
    except (AttributeError, ValueError):
        scheme = ""
    if scheme == "http":
        _validate_url(value, label, allow_internal_http=True)
        if not backchannel_base or not _is_beneath_base(value, backchannel_base):
            raise IssuerMisconfiguredError(
                f"OIDC {label} using HTTP must remain beneath the exact "
                "configured backchannel base URL."
            )
        return value
    return _validate_url(value, label)


def _validate_discovered_endpoint(name, value):
    """Validate an endpoint obtained from an OIDC discovery document."""
    auth_config = get_auth_config()
    issuer = auth_config["issuer"]
    backchannel_base = _get_backchannel_base()
    _validate_configured_endpoint(value, _ENDPOINT_KEYS[name])
    scheme = urlsplit(value).scheme.lower()
    if name in _FRONTCHANNEL_ENDPOINT_KEYS:
        if scheme != "https" or not _same_origin(value, issuer):
            raise IssuerMisconfiguredError(
                f"OIDC {_ENDPOINT_KEYS[name]} must use the configured public "
                "issuer origin. Configure an explicit endpoint override when "
                "the issuer intentionally uses another HTTPS origin."
            )
        return value
    if scheme == "https" and _same_origin(value, issuer):
        return value
    if backchannel_base and _is_beneath_base(value, backchannel_base):
        return value
    raise IssuerMisconfiguredError(
        f"OIDC {_ENDPOINT_KEYS[name]} is outside the configured issuer and "
        "backchannel trust boundaries."
    )


def validate_auth_configuration():
    """Fail fast when the application's OIDC transport policy is inconsistent."""
    auth_config = get_auth_config()
    issuer = auth_config.get("issuer", "")
    if not issuer:
        if auth_config.get("backchannel_base_url") or auth_config.get(
            "backchannel_allow_http"
        ):
            raise IssuerMisconfiguredError(
                "OIDC backchannel settings require an OIDC issuer."
            )
        return
    _validate_base_url(issuer, "issuer")
    required_values = {
        "audience": "access-token audience",
        "roles_claim": "roles claim",
        "required_role": "required role",
        "cli_client_id": "CLI client id",
    }
    for key, label in required_values.items():
        if not auth_config.get(key):
            raise IssuerMisconfiguredError(
                f"OIDC {label} must be configured when an issuer is enabled."
            )
    if auth_config.get("bff_enabled") and not auth_config.get("web_client_id"):
        raise IssuerMisconfiguredError(
            "OIDC web client id must be configured when browser login is enabled."
        )
    _get_backchannel_base()
    discovery_url = auth_config.get("openid_config_url")
    if discovery_url:
        _validate_configured_endpoint(discovery_url, "discovery URL")
    for name in _ENDPOINT_KEYS:
        explicit = auth_config.get(name)
        if explicit:
            _validate_configured_endpoint(explicit, name)


def _validate_discovery_document(document):
    """Validate issuer binding, required metadata, and advertised URLs."""
    if not isinstance(document, dict):
        raise IssuerMisconfiguredError(
            "Issuer's OIDC discovery document must be a JSON object."
        )
    auth_config = get_auth_config()
    configured_issuer = auth_config.get("issuer")
    if not configured_issuer:
        raise IssuerMisconfiguredError("OIDC issuer is not configured.")
    _validate_base_url(configured_issuer, "issuer")
    if document.get("issuer") != configured_issuer:
        raise IssuerMisconfiguredError(
            "Issuer's OIDC discovery document does not exactly match the "
            "configured issuer."
        )
    for field in _REQUIRED_URL_FIELDS:
        if field not in document:
            raise IssuerMisconfiguredError(
                f"Issuer's OIDC discovery document is missing required field '{field}'."
            )
    for field in _REQUIRED_LIST_FIELDS:
        if not isinstance(document.get(field), list) or not document[field]:
            raise IssuerMisconfiguredError(
                f"Issuer's OIDC discovery document has invalid required field '{field}'."
            )
    validated_document = dict(document)
    for name, field in _ENDPOINT_KEYS.items():
        explicit = auth_config.get(name)
        if explicit:
            validated_document[field] = _validate_configured_endpoint(explicit, name)
        elif field in validated_document:
            _validate_discovered_endpoint(name, validated_document[field])
    return validated_document


def get_openid_configuration_url():
    """Return the configured or issuer-derived OIDC discovery URL."""
    auth_config = get_auth_config()
    if auth_config["openid_config_url"]:
        return _validate_configured_endpoint(
            auth_config["openid_config_url"], "discovery URL"
        )
    if auth_config["issuer"]:
        issuer = _validate_base_url(auth_config["issuer"], "issuer")
        return issuer.rstrip("/") + "/.well-known/openid-configuration"
    raise IssuerMisconfiguredError(
        "OIDC issuer is not configured "
        "(set REANA_AUTH_ISSUER or REANA_AUTH_OPENID_CONFIG_URL)."
    )


def _get_discovery_state():
    """Return this application's discovery cache and its refresh coordinator."""
    extensions = current_app.extensions
    state = extensions.get(_DISCOVERY_EXTENSION)
    if state is None:
        lock = threading.Lock()
        state = extensions.setdefault(
            _DISCOVERY_EXTENSION,
            {
                "lock": lock,
                "condition": threading.Condition(lock),
                "doc": None,
                "fetched_at": 0.0,
                "failed_at": 0.0,
                "refresh_in_progress": False,
                "refresh_generation": 0,
            },
        )
    return state


def discovery_is_unavailable():
    """Return whether this application's discovery cache has no usable document.

    True only when there is no cached document (fresh or stale) *and* the
    most recent refresh attempt is known to have failed -- mirrors
    ``JWKSCache.is_unavailable``. Read-only: never triggers a fetch, so safe
    to call from a health check.
    """
    state = _get_discovery_state()
    with state["condition"]:
        return state["doc"] is None and bool(state["failed_at"])


def _discovery_refresh_wait_timeout():
    """Bound how long a caller waits on an in-flight refresh.

    Mirrors the JWKS cache so a stuck refresh cannot pin a synchronous worker
    indefinitely while still covering a full issuer round trip.
    """
    return max(30, 4 * get_auth_config()["http_timeout"] + 5)


def _fetch_discovery_document():
    """Fetch and validate the discovery document without holding the mutex.

    Mirrors ``JWKSCache._fetch``: on one of the expected failure families
    (transport, decoding, or issuer-response validation), the caller decides
    whether to fall back to a stale cached document. Any other exception
    propagates as-is for the caller's broader cleanup handler.

    ``IssuerMisconfiguredError`` is deliberately re-raised as-is ahead of the
    broader transport/decoding catch below, even though it is-an
    ``AuthError`` and would otherwise match that tuple too: it signals a
    permanent configuration defect (e.g. a malformed issuer URL or a
    discovery document that fails validation), not a transient issuer
    outage. Rewrapping it as ``IssuerUnavailableError`` here would make the
    caller treat it as transient -- retrying forever, or silently serving a
    stale cached document forever instead of surfacing the misconfiguration.
    """
    url = None
    try:
        url = get_openid_configuration_url()
        response = requests.get(url, **get_issuer_request_kwargs())
        raise_for_issuer_status(response)
        return _validate_discovery_document(response.json())
    except IssuerMisconfiguredError:
        raise
    except (requests.RequestException, ValueError, AuthError) as error:
        location = url or "the configured issuer"
        raise IssuerUnavailableError(
            f"Could not fetch OIDC discovery document from {location}: {error}"
        ) from error


def get_openid_configuration():
    """Return the issuer's OIDC discovery document (cached in-process).

    The issuer network fetch runs outside the cache mutex as a bounded
    single-flight refresh: one caller fetches while others reuse a usable
    document or wait for the in-flight refresh. A slow or unavailable issuer
    therefore cannot hold the process-wide lock underneath every concurrent
    authentication and BFF-refresh path.
    """
    state = _get_discovery_state()
    condition = state["condition"]
    with condition:
        now = time.monotonic()
        if state["doc"] is not None and now - state["fetched_at"] < _DISCOVERY_TTL:
            return state["doc"]
        if state["refresh_in_progress"]:
            # Another caller is already fetching. Reuse a usable document, or
            # wait for that refresh rather than starting a second network call.
            if state["doc"] is not None:
                return state["doc"]
            deadline = now + _discovery_refresh_wait_timeout()
            while state["refresh_in_progress"] and state["doc"] is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IssuerUnavailableError("OIDC discovery refresh timed out.")
                condition.wait(timeout=remaining)
            if state["doc"] is not None:
                return state["doc"]
            raise IssuerUnavailableError(
                "OIDC discovery document is temporarily unavailable."
            )
        if state["failed_at"] and now - state["failed_at"] < _DISCOVERY_FAILURE_TTL:
            if state["doc"] is not None:
                return state["doc"]
            raise IssuerUnavailableError(
                "OIDC discovery document is temporarily unavailable."
            )
        state["refresh_in_progress"] = True
        generation = state["refresh_generation"]

    try:
        doc = _fetch_discovery_document()
    except Exception as error:
        # Clean up single-flight state for every failure, not only the
        # expected families _fetch_discovery_document converts to
        # IssuerUnavailableError -- an unexpected exception here (a bug, an
        # unrelated library raising something else entirely) must not wedge
        # refresh_in_progress permanently. With max_worker_lifetime: 0
        # (time-based recycling disabled), a wedged worker would otherwise
        # keep making every discovery caller wait the full refresh timeout
        # (with no cached document) or silently keep serving one stale
        # document forever (with one), until the process happens to recycle
        # on request count.
        with condition:
            state["failed_at"] = time.monotonic()
            state["refresh_in_progress"] = False
            condition.notify_all()
            if state["doc"] is not None and isinstance(error, IssuerUnavailableError):
                # Keep serving the stale document rather than failing hard,
                # but only for the failure families _fetch_discovery_document
                # itself judged transient/issuer-side -- not for a genuinely
                # unexpected exception, which should surface rather than be
                # silently absorbed into "serve stale forever".
                logging.warning(
                    "Could not refresh OIDC discovery document, "
                    "serving cached document: %s",
                    error,
                )
                return state["doc"]
        raise

    with condition:
        if generation == state["refresh_generation"]:
            state["doc"] = doc
            state["fetched_at"] = time.monotonic()
            state["refresh_generation"] += 1
        state["failed_at"] = 0.0
        state["refresh_in_progress"] = False
        condition.notify_all()
        return state["doc"]


def get_endpoint(name):
    """Return an issuer endpoint URL by REANA_AUTH key (e.g. ``jwks_url``).

    Explicit configuration wins; otherwise the discovery document is used.
    """
    explicit = get_auth_config().get(name)
    if explicit:
        return _validate_configured_endpoint(explicit, name)
    discovery_field = _ENDPOINT_KEYS[name]
    endpoint = get_openid_configuration().get(discovery_field)
    if not endpoint:
        raise IssuerMisconfiguredError(
            f"Issuer's OIDC discovery document does not advertise "
            f"'{discovery_field}'."
        )
    return _validate_discovered_endpoint(name, endpoint)
