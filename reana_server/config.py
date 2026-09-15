# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask application configuration."""

import json
import logging
import os
import re
from datetime import timedelta
from typing import Optional
from urllib.parse import quote as _urlquote

from distutils.util import strtobool
from limits.util import parse
from reana_commons.config import (
    REANA_INFRASTRUCTURE_COMPONENTS_HOSTNAMES,
    WORKFLOW_SPECIFICATION_BUNDLES_CAPABILITY,
)
from reana_commons.job_utils import kubernetes_memory_to_bytes

# Database URI re-exported for Flask/SQLAlchemy consumers.
from reana_db.config import SQLALCHEMY_DATABASE_URI

SQLALCHEMY_TRACK_MODIFICATIONS = False
"""Track modifications flag."""


def compose_reana_url(hostname: str, hostport: str | int) -> str:
    """Compose a REANA URL while omitting the default port."""
    if str(hostport) == "443":
        return f"https://{hostname}"
    return f"https://{hostname}:{hostport}"


def _positive_seconds_or_none(value) -> Optional[int]:
    """Parse a positive-integer seconds setting, deferring invalid values.

    Returns ``None`` for anything that is not a positive whole number of
    seconds, so that the application factory can report every unusable
    operator setting the same way instead of failing at import time.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        # Helm renders large unquoted YAML numbers in scientific notation
        # (e.g. 2592000 -> "2.592e+06"), which ``int()`` cannot parse. Accept
        # any float-representable whole number of seconds so such a value does
        # not crash startup; ``inf``/``nan``/fractions still fail ``is_integer``.
        try:
            as_float = float(value)
        except (TypeError, ValueError):
            return None
        if not as_float.is_integer():
            return None
        seconds = int(as_float)
    if seconds <= 0:
        return None
    try:
        # A finite, whole-valued float (e.g. 1e100) passes the checks above
        # but is far larger than ``timedelta`` can represent; reject it here
        # instead of letting ``_gitlab_webhook_secret_expiry()`` raise
        # ``OverflowError`` the first time a webhook secret is issued.
        timedelta(seconds=seconds)
    except OverflowError:
        return None
    return seconds


ADMIN_USER_ID = "00000000-0000-0000-0000-000000000000"

SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/var/reana")

REANA_API_CAPABILITIES = [WORKFLOW_SPECIFICATION_BUNDLES_CAPABILITY]
"""Protocol capabilities advertised by the unauthenticated ``/api/ping``.

Capabilities, not version comparisons, decide whether a client operation is
supported. The list is additive: new protocols append a new string and released
clients keep ignoring the field."""

REANA_SPEC_BUNDLE_MAX_FILES = int(os.getenv("REANA_SPEC_BUNDLE_MAX_FILES", "1000"))
"""Maximum number of files accepted in an uploaded specification bundle.

Bounds the staging of an untrusted multipart upload so a client cannot exhaust
the shared volume with a huge bundle."""

REANA_SPEC_BUNDLE_MAX_BYTES = int(
    os.getenv("REANA_SPEC_BUNDLE_MAX_BYTES", str(100 * 1024 * 1024))
)
"""Maximum cumulative extracted file bytes in a specification bundle."""

REANA_SPEC_BUNDLE_MAX_PATH_BYTES = int(
    os.getenv("REANA_SPEC_BUNDLE_MAX_PATH_BYTES", "4096")
)
"""Maximum UTF-8 byte length of one specification-bundle member path."""

REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES = int(
    os.getenv(
        "REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES",
        str(
            REANA_SPEC_BUNDLE_MAX_BYTES
            + REANA_SPEC_BUNDLE_MAX_FILES * (2 * REANA_SPEC_BUNDLE_MAX_PATH_BYTES + 128)
            + 64 * 1024
        ),
    )
)
"""Maximum multipart request bytes for a specification bundle.

This is deliberately larger than the extracted-content limit: an uncompressed
ZIP stores every member name in both its local and central-directory records,
and multipart framing adds a small fixed overhead. Member-count and path-length
limits keep this allowance bounded.
"""

REANA_SPEC_VALIDATION_TIMEOUT = int(os.getenv("REANA_SPEC_VALIDATION_TIMEOUT", "60"))
"""Wall-clock budget (seconds) used to size the server's read timeout for a
sandboxed spec validation call.

Read from the same ``REANA_SPEC_VALIDATION_TIMEOUT`` environment variable as
reana-workflow-controller, which owns the actual sandbox Job
``activeDeadlineSeconds``; the default matches the controller's so the two agree
when the variable is unset. This value is *not* the deadline itself -- the
server derives a longer read timeout from it (``+120s``, above the controller's
``timeout + 60`` wait) so it never gives up before the controller's sandbox
deadline elapses."""

REANA_HOSTNAME = os.getenv("REANA_HOSTNAME", "localhost")
REANA_HOSTPORT = os.getenv("REANA_HOSTPORT", "30443")
REANA_URL = compose_reana_url(REANA_HOSTNAME, REANA_HOSTPORT)

DASK_ENABLED = strtobool(os.getenv("DASK_ENABLED", "true"))
"""Whether Dask is enabled in the cluster or not."""

DASK_AUTOSCALER_ENABLED = os.getenv("DASK_AUTOSCALER_ENABLED", "true").lower() == "true"
"""Whether Dask autoscaler is enabled in the cluster or not."""

REANA_DASK_CLUSTER_MAX_MEMORY_LIMIT = os.getenv(
    "REANA_DASK_CLUSTER_MAX_MEMORY_LIMIT", "16Gi"
)
"""Maximum memory limit for Dask clusters."""

REANA_DASK_CLUSTER_DEFAULT_NUMBER_OF_WORKERS = int(
    os.getenv("REANA_DASK_CLUSTER_DEFAULT_NUMBER_OF_WORKERS", 2)
)
"""Number of workers in Dask cluster by default."""

REANA_DASK_CLUSTER_MAX_NUMBER_OF_WORKERS = int(
    os.getenv("REANA_DASK_CLUSTER_MAX_NUMBER_OF_WORKERS", 20)
)
"""Maximum number of workers in Dask cluster."""

REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_MEMORY = os.getenv(
    "REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_MEMORY", "2Gi"
)
"""Memory for one Dask worker by default."""

REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_MEMORY = os.getenv(
    "REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_MEMORY", "8Gi"
)
"""Maximum memory for one Dask worker."""

REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_THREADS = int(
    os.getenv("REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_THREADS", 4)
)
"""Number of threads for one Dask worker by default."""

REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_THREADS = int(
    os.getenv("REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_THREADS", 8)
)
"""Maximum number of threads for one Dask worker."""

REANA_QUOTA_MANAGEMENT_SECRET = os.getenv("REANA_QUOTA_MANAGEMENT_SECRET", "")
"""Secret used to authenticate quota-management REST API requests."""

REANA_KUBERNETES_JOBS_CPU_REQUEST = os.getenv("REANA_KUBERNETES_JOBS_CPU_REQUEST")
"""Default cpu request for user job containers."""

REANA_KUBERNETES_JOBS_CPU_LIMIT = os.getenv("REANA_KUBERNETES_JOBS_CPU_LIMIT")
"""Default cpu limit for user job containers."""

REANA_KUBERNETES_JOBS_MEMORY_REQUEST = os.getenv("REANA_KUBERNETES_JOBS_MEMORY_REQUEST")
"""Default memory request for user job containers."""

REANA_KUBERNETES_JOBS_MEMORY_LIMIT = os.getenv("REANA_KUBERNETES_JOBS_MEMORY_LIMIT")
"""Default memory limit for user job containers."""

REANA_KUBERNETES_JOBS_MAX_USER_CPU_REQUEST = os.getenv(
    "REANA_KUBERNETES_JOBS_MAX_USER_CPU_REQUEST"
)
"""Maximum cpu request that users can assign to their job containers."""

REANA_KUBERNETES_JOBS_MAX_USER_CPU_LIMIT = os.getenv(
    "REANA_KUBERNETES_JOBS_MAX_USER_CPU_LIMIT"
)
"""Maximum cpu limit that users can assign to their job containers."""

REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_REQUEST = os.getenv(
    "REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_REQUEST"
)
"""Maximum memory request that users can assign to their job containers."""

REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT = os.getenv(
    "REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT"
)
"""Maximum memory limit that users can assign to their job containers."""


REANA_KUBERNETES_JOBS_MEMORY_LIMIT_IN_BYTES = (
    kubernetes_memory_to_bytes(REANA_KUBERNETES_JOBS_MEMORY_LIMIT)
    if REANA_KUBERNETES_JOBS_MEMORY_LIMIT
    else 0
)
"""Maximum memory limit for user job containers in bytes."""

REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT_IN_BYTES = (
    kubernetes_memory_to_bytes(REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT)
    if REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT
    else 0
)
"""Maximum memory limit that users can assign to their job containers in bytes."""

REANA_WORKFLOW_SCHEDULING_POLICY = os.getenv("REANA_WORKFLOW_SCHEDULING_POLICY", "fifo")

REANA_WORKFLOW_SCHEDULING_POLICIES = ["fifo", "balanced"]
"""REANA workflow scheduling policies.
- ``fifo``: first-in first-out strategy starting workflows as they come.
- ``balanced``: a weighted strategy taking into account existing multi-user workloads and the DAG complexity of incoming workflows.
"""

REANA_WORKFLOW_SCHEDULING_READINESS_CHECK_LEVEL = int(
    os.getenv("REANA_WORKFLOW_SCHEDULING_READINESS_CHECK_LEVEL", 9)
)
"""REANA workflow scheduling readiness check needed to assess whether the cluster is ready to start new workflows."""

REANA_WORKFLOW_SCHEDULING_READINESS_CHECK_LEVEL_VALUE_MAP = {
    0: "no_checks",
    1: "concurrent",
    2: "memory",
    9: "all_checks",
}
"""REANA workflow scheduling readiness check level value map:
- 0 = no readiness check; schedule new workflow as soon as they arrive;
- 1 = check for maximum number of concurrently running workflows; schedule new workflows if not exceeded;
- 2 = check for available cluster memory size; schedule new workflow only if it fits;
- 9 = perform all checks; satisfy all previous criteria.
"""

REANA_WORKFLOW_SCHEDULING_READINESS_CHECK_VALUE = (
    REANA_WORKFLOW_SCHEDULING_READINESS_CHECK_LEVEL_VALUE_MAP.get(
        REANA_WORKFLOW_SCHEDULING_READINESS_CHECK_LEVEL, "all_checks"
    )
)
"""REANA workflow scheduling readiness check value."""

SUPPORTED_COMPUTE_BACKENDS = json.loads(os.getenv("REANA_COMPUTE_BACKENDS", "[]")) or []
"""List of supported compute backends."""

REANA_QUOTAS_DOCS_URL = "https://docs.reana.io/advanced-usage/user-quotas"


# CORS
# ====
REST_ENABLE_CORS = True
# See: https://flask-cors.readthedocs.io/en/latest/configuration.html
# Echo the request's origin back only if it matches CORS_ORIGINS.
# If True, it would send Access-Control-Allow-Origin: *
# unconditionally, and let any website read API responses in the browser
CORS_SEND_WILDCARD = False
CORS_SUPPORTS_CREDENTIALS = False
CORS_ORIGINS = [REANA_URL]

# Flask configuration
# ===================
# See details on
# http://flask.pocoo.org/docs/0.12/config/#builtin-configuration-values

#: Secret key - each installation (dev, production, ...) needs a separate key.
#: It should be changed before deploying.
SECRET_KEY = os.getenv("REANA_SECRET_KEY", "")
"""Secret key used for the application user sessions."""

#: Since HAProxy and Nginx route all requests no matter the host header
#: provided, the allowed hosts variable is set to localhost. In production it
#: should be set to the correct host and it is strongly recommended to only
#: route correct hosts to the application.

#: In production use the following configuration plus adding  the hostname/ip
#: of the reverse proxy in front of REANA-Server.
if REANA_HOSTNAME:
    TRUSTED_HOSTS = [REANA_HOSTNAME]

# Security configuration
# ======================
PROXYFIX_CONFIG = json.loads(os.getenv("PROXYFIX_CONFIG", '{"x_proto": 1}'))
APP_DEFAULT_SECURE_HEADERS = {
    "force_https": False,
    "frame_options": "DENY",
    "strict_transport_security": True,
    "strict_transport_security_max_age": 31536000,
    "strict_transport_security_include_subdomains": True,
    "referrer_policy": "strict-origin-when-cross-origin",
    "content_security_policy": {
        "default-src": ["'self'"],
        "script-src": ["'self'"],
        "style-src": ["'self'", "'unsafe-inline'"],
        "img-src": ["'self'", "data:"],
        "font-src": ["'self'"],
        "connect-src": ["'self'"],
        "frame-ancestors": ["'none'"],
        "object-src": ["'none'"],
        "base-uri": ["'self'"],
    },
}
APP_DEFAULT_SECURE_HEADERS.update(
    json.loads(os.getenv("APP_DEFAULT_SECURE_HEADERS", "{}"))
)
if "REANA_FORCE_HTTPS" in os.environ:
    APP_DEFAULT_SECURE_HEADERS["force_https"] = bool(
        strtobool(os.getenv("REANA_FORCE_HTTPS"))
    )

APP_HEALTH_BLUEPRINT_ENABLED = False


# Rate limiting configuration
# ===========================


def _get_rate_limit(env_variable: str, default: str) -> str:
    env_value = os.getenv(env_variable)
    if not env_value:
        return default
    try:
        parse(env_value)
        return env_value
    except ValueError:
        # Distinct from the common "not set at all" case above: this is a
        # value the operator explicitly set that Flask-Limiter cannot parse
        # (e.g. a typo), silently falling back to the default with no
        # signal that the override never took effect.
        logging.warning(
            "%s=%r is not a valid rate limit; using the default %r instead.",
            env_variable,
            env_value,
            default,
        )
        return default


def _get_int_env_variable(env_variable: str, default: int) -> int:
    """Return an integer environment variable value or fall back to default."""
    env_value = os.getenv(env_variable)
    if env_value is None:
        return default
    try:
        return int(env_value)
    except ValueError:
        logging.warning(
            "Invalid %s=%r; falling back to %s.",
            env_variable,
            env_value,
            default,
        )
        return default


RATELIMIT_GUEST_USER = _get_rate_limit("REANA_RATELIMIT_GUEST_USER", "20 per second")
RATELIMIT_AUTHENTICATED_USER = _get_rate_limit(
    "REANA_RATELIMIT_AUTHENTICATED_USER", "20 per second"
)
REANA_RATELIMIT_SLOW = _get_rate_limit("REANA_RATELIMIT_SLOW", "1/5 second")
REANA_RATELIMIT_SLOWER = _get_rate_limit("REANA_RATELIMIT_SLOWER", "30 per minute")
REANA_RATELIMIT_SLOWEST = _get_rate_limit("REANA_RATELIMIT_SLOWEST", "5 per hour")

RATELIMIT_PER_ENDPOINT = {
    "auth.login": REANA_RATELIMIT_SLOWER,
    "auth.oauth_callback": REANA_RATELIMIT_SLOWER,
    "auth.logout": REANA_RATELIMIT_SLOWER,
    "launch.launch": REANA_RATELIMIT_SLOW,
    # Both endpoints can spawn a sandboxed validation Job per call (for
    # non-serial specs), so throttle them like ``launch`` to bound the rate at
    # which an authenticated user can drive validator Jobs against the cluster.
    "workflows.validate_workflow_specification": REANA_RATELIMIT_SLOW,
    "workflows.create_workflow": REANA_RATELIMIT_SLOW,
    "workflows.start_workflow": REANA_RATELIMIT_SLOW,
    # A restart re-loads and re-validates the workspace, which can spawn a
    # sandboxed validation Job for non-serial specs, so throttle it like start.
    "workflows.restart_workflow": REANA_RATELIMIT_SLOW,
    "users.request_token": REANA_RATELIMIT_SLOWEST,
}


# Gitlab Application configuration
# ================================
REANA_GITLAB_OAUTH_APP_ID = os.getenv("REANA_GITLAB_OAUTH_APP_ID", "")
REANA_GITLAB_OAUTH_APP_SECRET = os.getenv("REANA_GITLAB_OAUTH_APP_SECRET", "")
REANA_GITLAB_HOST = os.getenv("REANA_GITLAB_HOST", "")
REANA_GITLAB_URL = "https://{}".format(REANA_GITLAB_HOST) if REANA_GITLAB_HOST else ""
REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME = os.getenv(
    "REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME", "2592000"
)
"""Raw maximum lifetime for a delegated GitLab webhook secret.

The application factory validates this value and replaces it in ``app.config``
with a positive integer. Keeping the raw environment value here lets every
entry point report a consistent startup error instead of raising at import time.
"""

REANA_GITLAB_WEBHOOK_SSL_VERIFICATION = bool(
    strtobool(os.getenv("REANA_GITLAB_WEBHOOK_SSL_VERIFICATION", "true"))
)
"""Whether GitLab authenticates REANA's TLS certificate when delivering webhooks.

Defaults to ``True`` so the delegated webhook secret is never delivered over an
unauthenticated transport where an on-path attacker could capture it. Self-signed
local development installs may set this to ``false``; production private-PKI
deployments should instead configure GitLab to trust the relevant CA.
"""

# Workflow scheduler
# ==================
REANA_SCHEDULER_REQUEUE_SLEEP = float(os.getenv("REANA_SCHEDULER_REQUEUE_SLEEP", "15"))
"""How many seconds to wait between consuming workflows."""

REANA_SCHEDULER_REQUEUE_COUNT = float(os.getenv("REANA_SCHEDULER_REQUEUE_COUNT", "200"))
"""How many times to requeue workflow, in case of error or busy cluster, before failing it."""

# Workflow fetcher
# ================
WORKFLOW_SPEC_FILENAMES = ["reana.yaml", "reana.yml"]
"""Filenames to use when discovering workflow specifications."""

WORKFLOW_SPEC_EXTENSIONS = [".yaml", ".yml"]
"""Valid file extensions of workflow specifications."""

REGEX_CHARS_TO_REPLACE = re.compile("[^a-zA-Z0-9_]+")
"""Regex matching groups of characters that need to be replaced in workflow names."""

FETCHER_MAXIMUM_FILE_SIZE = int(
    os.getenv("REANA_FETCHER_MAXIMUM_FILE_SIZE", str(1024**3))
)
"""Maximum file size allowed when fetching workflow specifications."""

FETCHER_MAXIMUM_EXTRACTED_SIZE = int(
    os.getenv("REANA_FETCHER_MAXIMUM_EXTRACTED_SIZE", str(1024**3))
)
"""Maximum cumulative regular-file bytes extracted from a remote archive."""

FETCHER_MAXIMUM_FILES = int(os.getenv("REANA_FETCHER_MAXIMUM_FILES", "10000"))
"""Maximum number of regular files accepted from a remote source snapshot."""

FETCHER_MAXIMUM_CLONE_SIZE = int(
    os.getenv(
        "REANA_FETCHER_MAXIMUM_CLONE_SIZE",
        str(FETCHER_MAXIMUM_FILE_SIZE + FETCHER_MAXIMUM_EXTRACTED_SIZE),
    )
)
"""Maximum temporary bytes used by the generic Git fallback clone."""

FETCHER_ALLOWED_SCHEMES = ["https", "http"]
"""Schemes allowed when fetching workflow specifications."""

FETCHER_REQUEST_TIMEOUT = 60
"""Timeout used when fetching workflow specifications."""

RWC_MUTATION_CONNECT_TIMEOUT = float(
    os.getenv("REANA_RWC_MUTATION_CONNECT_TIMEOUT", "10")
)
"""Connection timeout for controller calls made under workspace locks."""

RWC_MUTATION_READ_TIMEOUT = float(os.getenv("REANA_RWC_MUTATION_READ_TIMEOUT", "300"))
"""Read timeout for controller calls made under workspace locks."""

FETCHER_ALLOWED_GITLAB_HOSTNAMES = {"gitlab.com", "gitlab.cern.ch"}
if REANA_GITLAB_HOST:
    FETCHER_ALLOWED_GITLAB_HOSTNAMES.add(REANA_GITLAB_HOST)
"""GitLab instances allowed when fetching workflow specifications."""

# Workspace retention rules
# ==================
_workspace_retention_period_env = os.getenv("WORKSPACE_RETENTION_PERIOD", "forever")
if _workspace_retention_period_env == "forever":
    WORKSPACE_RETENTION_PERIOD: Optional[int] = None
else:
    WORKSPACE_RETENTION_PERIOD: Optional[int] = int(_workspace_retention_period_env)
"""Maximum allowed period for workspace retention rules.
The value "forever" means "do not apply any rules to files by default", and it is represented by None.
"""

DEFAULT_WORKSPACE_RETENTION_RULE = "**/*"
"""Workspace retention rule which will be applied to all the workflows by default."""

# Interactive sessions configuration
# ==================
_reana_interactive_session_max_inactivity_period_env = os.getenv(
    "REANA_INTERACTIVE_SESSION_MAX_INACTIVITY_PERIOD", "forever"
)
if _reana_interactive_session_max_inactivity_period_env == "forever":
    REANA_INTERACTIVE_SESSION_MAX_INACTIVITY_PERIOD: Optional[str] = None
else:
    REANA_INTERACTIVE_SESSION_MAX_INACTIVITY_PERIOD: Optional[str] = (
        _reana_interactive_session_max_inactivity_period_env
    )
"""Maximum allowed period (in days) for interactive session inactivity before automatic closure."""

REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS = json.loads(
    os.getenv("REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS", "{}")
)
"""Allowed and recommended environments to be used for interactive sessions."""

REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS_CUSTOM_ALLOWED = (
    str(
        REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS.get("jupyter", {}).get(
            "allow_custom", "false"
        )
    ).lower()
    == "true"
)
"""Whether users can set custom interactive session images or not."""

REANA_VETTED_CONTAINER_IMAGES = json.loads(
    os.getenv(
        "REANA_VETTED_CONTAINER_IMAGES",
        '{"enabled": false, "allowlist": []}',
    )
)
"""Container images that users are allowed to use in their workflows."""

# Kubernetes jobs timeout
# ==================
REANA_KUBERNETES_JOBS_TIMEOUT_LIMIT = os.getenv("REANA_KUBERNETES_JOBS_TIMEOUT_LIMIT")
"""Default timeout for user's jobs in seconds. Exceeding this time will terminate the job.

Please see the following URL for more details
https://kubernetes.io/docs/concepts/workloads/controllers/job/#job-termination-and-cleanup.
"""

REANA_KUBERNETES_JOBS_MAX_USER_TIMEOUT_LIMIT = os.getenv(
    "REANA_KUBERNETES_JOBS_MAX_USER_TIMEOUT_LIMIT"
)
"""Maximum custom timeout in seconds that users can assign to their jobs.

Please see the following URL for more details
https://kubernetes.io/docs/concepts/workloads/controllers/job/#job-termination-and-cleanup.
"""

REANA_KUBERNETES_JOBS_MIN_USER_UID = _get_int_env_variable(
    "REANA_KUBERNETES_JOBS_MIN_USER_UID", 100
)
"""Minimum accepted user runtime container UID that users can assign to their job
containers via ``kubernetes_uid`` in ``reana.yaml``. Jobs requesting a smaller
UID are refused at submission time with a clear error message. Surfaced via
the ``/info`` endpoint so that users and administrators can verify the
configured value.
"""

# OIDC/JWT authentication configuration
# =====================================
_AUTH_HTTP_TIMEOUT = int(os.getenv("REANA_AUTH_HTTP_TIMEOUT", "10"))
"""Timeout (seconds) for HTTP calls to the issuer."""

_AUTH_REFRESH_WAIT_TIMEOUT = float(
    os.getenv("REANA_AUTH_REFRESH_WAIT_TIMEOUT", "")
    or min(15.0, max(5.0, float(_AUTH_HTTP_TIMEOUT)))
)
"""Derived default for how long a request waits out a concurrent refresh.

One issuer round trip's worth of patience, floored so a very small issuer
timeout still tolerates ordinary latency and capped so a synchronous worker is
never held for long. Operators can still pin an explicit value.
"""

REANA_AUTH = {
    # The single trusted OIDC issuer (the bundled Keycloak realm by
    # default, any OIDC-compliant issuer otherwise), e.g.
    # "https://auth.reana.example.org/realms/reana".
    "issuer": os.getenv("REANA_AUTH_ISSUER", ""),
    # Expected audience(s) of access tokens, comma-separated. A token is
    # accepted when its `aud` contains at least one of these -- needed when
    # separate CLI and web clients are registered with the issuer, since
    # each mints tokens audienced to its own client id. An empty list here
    # is not a supported "skip audience checking" mode: whenever an issuer
    # is configured, discovery.validate_auth_configuration() rejects a
    # missing audience at startup (fails closed), so token validation never
    # actually runs with an empty audience list on a live server.
    "audience": [
        value
        for value in (
            v.strip() for v in os.getenv("REANA_AUTH_AUDIENCE", "reana").split(",")
        )
        if value
    ],
    # Endpoint overrides; when empty, endpoints are resolved from the
    # issuer's /.well-known/openid-configuration document.
    "openid_config_url": os.getenv("REANA_AUTH_OPENID_CONFIG_URL", ""),
    # Optional physical transport base used for server-to-issuer calls. It is
    # independent from the stable public issuer identifier. HTTP is accepted
    # only below this exact base and only with the explicit opt-in below.
    "backchannel_base_url": os.getenv("REANA_AUTH_BACKCHANNEL_BASE_URL", ""),
    "backchannel_allow_http": bool(
        strtobool(os.getenv("REANA_AUTH_BACKCHANNEL_ALLOW_HTTP", "false"))
    ),
    "jwks_url": os.getenv("REANA_AUTH_JWKS_URL", ""),
    "userinfo_url": os.getenv("REANA_AUTH_USERINFO_URL", ""),
    # Public client id used by reana-client for the device authorization
    # grant; advertised through the openid-configuration proxy endpoint.
    "cli_client_id": os.getenv("REANA_AUTH_CLIENT_ID", "reana-cli"),
    # Claim carrying REANA roles.
    "roles_claim": os.getenv("REANA_AUTH_ROLES_CLAIM", "reana_roles"),
    # Role required to use protected API endpoints. Authentication-enabled
    # applications fail at startup when this is empty.
    "required_role": os.getenv("REANA_AUTH_REQUIRED_ROLE", "reana:user"),
    # Clock-skew leeway (seconds) for exp/nbf validation.
    "leeway": int(os.getenv("REANA_AUTH_LEEWAY", "30")),
    # TTL (seconds) of the in-process JWKS cache. The discovery-document
    # cache has its own fixed TTL (auth/discovery.py's _DISCOVERY_TTL); this
    # setting does not apply to it.
    "jwks_ttl": int(os.getenv("REANA_AUTH_JWKS_TTL", "600")),
    # Additional time (seconds) that a previously fetched JWKS may be used
    # after its normal TTL when a transient issuer refresh fails. This bounds
    # the availability fallback so a key removed by the issuer cannot remain
    # trusted indefinitely during a prolonged outage.
    "jwks_stale_grace": int(os.getenv("REANA_AUTH_JWKS_STALE_GRACE", "3600")),
    # Same bound as jwks_stale_grace, but for the discovery document cache.
    # Without this, an issuer that rotates an endpoint (e.g. jwks_uri, as
    # part of decommissioning a compromised one) while its discovery refresh
    # happens to be failing would have REANA keep resolving the old,
    # possibly-decommissioned URL indefinitely -- the JWKS cache's own
    # staleness bound does not help if the endpoint it fetches from is
    # itself stale.
    "discovery_stale_grace": int(os.getenv("REANA_AUTH_DISCOVERY_STALE_GRACE", "3600")),
    # Timeout (seconds) for HTTP calls to the issuer.
    "http_timeout": _AUTH_HTTP_TIMEOUT,
    # Optional CA bundle for a private/self-signed issuer certificate. TLS
    # verification cannot be disabled; an empty value uses the system roots.
    "ca_bundle": os.getenv("REANA_AUTH_CA_BUNDLE", ""),
    # BFF (backend-for-frontend) browser login: when enabled (and an issuer
    # is configured), reana-server runs the authorization code flow and
    # gives browsers httpOnly-cookie transport for the access JWT.
    "bff_enabled": bool(strtobool(os.getenv("REANA_AUTH_BFF_ENABLED", "true"))),
    # Confidential web client used by the BFF code flow.
    "web_client_id": os.getenv("REANA_AUTH_WEB_CLIENT_ID", "reana-server"),
    "web_client_secret": os.getenv("REANA_AUTH_WEB_CLIENT_SECRET", ""),
    # Scopes requested in the authorization code / device authorization flow.
    "scopes": os.getenv("REANA_AUTH_SCOPES", "openid profile email"),
    # Server-side lifetime (seconds) of a BFF session (refresh-token
    # storage in Redis); the issuer's session policy is the real authority.
    "session_ttl": int(os.getenv("REANA_AUTH_SESSION_TTL", "604800")),
    # Redis storage for BFF refresh tokens. Credentials are percent-quoted
    # so operator-supplied passwords cannot break URI parsing.
    "redis_url": os.getenv("REANA_AUTH_REDIS_URL", "")
    or "redis://{user}:{password}@{host}:6379/1".format(
        user=_urlquote(os.getenv("REANA_CACHE_USER", ""), safe=""),
        password=_urlquote(os.getenv("REANA_CACHE_PASSWORD", ""), safe=""),
        host=REANA_INFRASTRUCTURE_COMPONENTS_HOSTNAMES["cache"],
    ),
    # Bound every Redis operation so a stalled cache cannot hold a serving
    # thread indefinitely. Health checks avoid reusing dead pooled sockets.
    "redis_socket_connect_timeout": float(
        os.getenv("REANA_AUTH_REDIS_SOCKET_CONNECT_TIMEOUT", "2")
    ),
    "redis_socket_timeout": float(os.getenv("REANA_AUTH_REDIS_SOCKET_TIMEOUT", "2")),
    # Maximum time a request waits for another worker to finish refreshing the
    # same browser session. This is independent from the longer Redis lock TTL,
    # which protects refresh-token rotation if the lock owner dies: waiting
    # that long would pin a synchronous worker whenever the lock owner is
    # killed. The default tracks the configured issuer timeout so that raising
    # it for a slow identity provider does not start returning 503 to sibling
    # browser tabs while the winning request is still legitimately in flight.
    "refresh_wait_timeout": _AUTH_REFRESH_WAIT_TIMEOUT,
    "redis_health_check_interval": int(
        os.getenv("REANA_AUTH_REDIS_HEALTH_CHECK_INTERVAL", "30")
    ),
    # Optional endpoint overrides (resolved from the discovery document
    # when empty, see reana_server.auth.discovery).
    "authorization_url": os.getenv("REANA_AUTH_AUTHORIZATION_URL", ""),
    "token_url": os.getenv("REANA_AUTH_TOKEN_URL", ""),
    "end_session_url": os.getenv("REANA_AUTH_END_SESSION_URL", ""),
    "device_authorization_url": os.getenv("REANA_AUTH_DEVICE_AUTHORIZATION_URL", ""),
    # Automatic linking of a freshly-seen IdP identity to a pre-existing
    # REANA account that has the same verified email (migration aid). It is
    # an account-takeover vector when the issuer does not truly verify
    # emails, so it is DISABLED by default and gated by explicit allow-lists
    # for migrations. With an empty allow-list the corresponding check is
    # skipped, so enabling linking
    # without any allow-list trusts every configured issuer/domain.
    "email_linking_enabled": bool(
        strtobool(os.getenv("REANA_AUTH_EMAIL_LINKING_ENABLED", "false"))
    ),
    "email_linking_issuer_allowlist": [
        value.strip()
        for value in os.getenv("REANA_AUTH_EMAIL_LINKING_ISSUER_ALLOWLIST", "").split(
            ","
        )
        if value.strip()
    ],
    "email_linking_domain_allowlist": [
        value.strip().lower()
        for value in os.getenv("REANA_AUTH_EMAIL_LINKING_DOMAIN_ALLOWLIST", "").split(
            ","
        )
        if value.strip()
    ],
    # Some institutional issuers (e.g. CERN Keycloak) never emit the
    # standard OIDC `email_verified` claim at all, even though the email
    # they assert is institutionally verified out-of-band (there is no
    # self-service "add any email" step on that issuer). Listing an issuer
    # here is a deliberate administrator attestation that its emails are
    # trustworthy without that claim; it does not weaken the check for any
    # other issuer.
    "email_linking_assume_verified_issuers": [
        value.strip()
        for value in os.getenv(
            "REANA_AUTH_EMAIL_LINKING_ASSUME_VERIFIED_ISSUERS", ""
        ).split(",")
        if value.strip()
    ],
}
"""OIDC/JWT authentication configuration."""
