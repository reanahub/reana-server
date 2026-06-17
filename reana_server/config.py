# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask application configuration."""

import copy
import json
import logging
import os
import re
from datetime import timedelta
from typing import Optional
from urllib.parse import quote as _urlquote

from distutils.util import strtobool
from limits.util import parse
from reana_commons.config import REANA_INFRASTRUCTURE_COMPONENTS_HOSTNAMES
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


ADMIN_USER_ID = "00000000-0000-0000-0000-000000000000"

SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/var/reana")

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
# CORS is configured on the FastAPI app via ``CORSMiddleware`` in
# ``reana_server.asgi``, restricted to ``REANA_URL`` (mirroring PR #766); the
# former flask-cors settings are no longer read by anything.

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

# HTTP security headers are set by the ASGI security-headers middleware in
# ``reana_server.asgi`` (the FastAPI app), mirroring PR #766. The Invenio /
# flask-talisman ``APP_DEFAULT_SECURE_HEADERS`` configuration was dropped
# together with Invenio and is no longer read by anything.

APP_HEALTH_BLUEPRINT_ENABLED = False


# Rate limiting configuration using invenio-app
# ===========================


def _get_rate_limit(env_variable: str, default: str) -> str:
    env_value = os.getenv(env_variable, "")
    try:
        parse(env_value)
        return env_value
    except ValueError:
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


def _get_json_env_variable(env_variable: str, default):
    """Return a JSON environment variable value or fail with context."""
    raw_value = os.getenv(env_variable)
    if raw_value is None:
        return default
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{env_variable} is not valid JSON: {error}") from error


# Note: users that are connecting via reana-client will be treated as guests by the Invenio framework
RATELIMIT_GUEST_USER = _get_rate_limit("REANA_RATELIMIT_GUEST_USER", "20 per second")
RATELIMIT_AUTHENTICATED_USER = _get_rate_limit(
    "REANA_RATELIMIT_AUTHENTICATED_USER", "20 per second"
)
REANA_RATELIMIT_SLOW = _get_rate_limit("REANA_RATELIMIT_SLOW", "1/5 second")
REANA_RATELIMIT_SLOWER = _get_rate_limit("REANA_RATELIMIT_SLOWER", "30 per minute")
REANA_RATELIMIT_SLOWEST = _get_rate_limit("REANA_RATELIMIT_SLOWEST", "5 per hour")

RATELIMIT_PER_ENDPOINT = {
    "launch.launch": REANA_RATELIMIT_SLOW,
}

# Gitlab Application configuration
# ================================
REANA_GITLAB_OAUTH_APP_ID = os.getenv("REANA_GITLAB_OAUTH_APP_ID", "")
REANA_GITLAB_OAUTH_APP_SECRET = os.getenv("REANA_GITLAB_OAUTH_APP_SECRET", "")
REANA_GITLAB_HOST = os.getenv("REANA_GITLAB_HOST", "")
REANA_GITLAB_URL = "https://{}".format(REANA_GITLAB_HOST) if REANA_GITLAB_HOST else ""

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

FETCHER_MAXIMUM_FILE_SIZE = 1024**3  # 1 GB
"""Maximum file size allowed when fetching workflow specifications."""

FETCHER_ALLOWED_SCHEMES = ["https", "http"]
"""Schemes allowed when fetching workflow specifications."""

FETCHER_REQUEST_TIMEOUT = 60
"""Timeout used when fetching workflow specifications."""

FETCHER_ALLOWED_GITLAB_HOSTNAMES = {"gitlab.com", "gitlab.cern.ch"}
if REANA_GITLAB_HOST:
    FETCHER_ALLOWED_GITLAB_HOSTNAMES.add(REANA_GITLAB_HOST)
"""GitLab instances allowed when fetching workflow specifications."""

LAUNCHER_ALLOWED_SNAKEMAKE_URLS = [
    "https://github.com/reanahub/reana-demo-cms-h4l",
    "https://github.com/reanahub/reana-demo-helloworld",
    "https://github.com/reanahub/reana-demo-root6-roofit",
    "https://github.com/reanahub/reana-demo-worldpopulation",
]
"""Allowed URLs when launching a Snakemake workflow."""

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
REANA_AUTH = {
    # The single trusted OIDC issuer (the bundled Keycloak realm by
    # default, any OIDC-compliant issuer otherwise), e.g.
    # "https://auth.reana.example.org/realms/reana".
    "issuer": os.getenv("REANA_AUTH_ISSUER", ""),
    # Expected audience of access tokens. Empty string disables audience
    # checking (needed for issuers whose audience cannot be configured).
    "audience": os.getenv("REANA_AUTH_AUDIENCE", "reana"),
    # Endpoint overrides; when empty, endpoints are resolved from the
    # issuer's /.well-known/openid-configuration document.
    "openid_config_url": os.getenv("REANA_AUTH_OPENID_CONFIG_URL", ""),
    "jwks_url": os.getenv("REANA_AUTH_JWKS_URL", ""),
    "userinfo_url": os.getenv("REANA_AUTH_USERINFO_URL", ""),
    # Public client id used by reana-client for the device authorization
    # grant; advertised through the openid-configuration proxy endpoint.
    "cli_client_id": os.getenv("REANA_AUTH_CLIENT_ID", "reana-cli"),
    # Claim carrying REANA roles, and optional provider-specific role sources
    # that can map issuer claims to REANA roles without code changes. The
    # source list is JSON, e.g.
    # [{"path":"resource_access.reana.roles",
    #   "map":{"user":"reana:user","admin":"reana:admin"}}].
    # The flat roles claim remains the default source for bundled Keycloak.
    "roles_claim": os.getenv("REANA_AUTH_ROLES_CLAIM", "reana_roles"),
    "role_sources": _get_json_env_variable(
        "REANA_AUTH_ROLE_SOURCES",
        [{"path": os.getenv("REANA_AUTH_ROLES_CLAIM", "reana_roles")}],
    ),
    # Role required to use protected API endpoints. An empty required role
    # disables the gate.
    "required_role": os.getenv("REANA_AUTH_REQUIRED_ROLE", "reana:user"),
    # Clock-skew leeway (seconds) for exp/nbf validation.
    "leeway": int(os.getenv("REANA_AUTH_LEEWAY", "30")),
    # TTL (seconds) of the in-process JWKS and discovery-document caches.
    "jwks_ttl": int(os.getenv("REANA_AUTH_JWKS_TTL", "600")),
    # Timeout (seconds) for HTTP calls to the issuer.
    "http_timeout": int(os.getenv("REANA_AUTH_HTTP_TIMEOUT", "10")),
    # BFF (backend-for-frontend) browser login: when enabled (and an issuer
    # is configured), reana-server runs the authorization code flow and
    # gives browsers httpOnly-cookie transport for the access JWT
    # (AUTH_ARCHITECTURE.md §5.1). Platform-embedded deployments that bring
    # their own tokens (e.g. the ESCAPE VRE) disable it.
    "bff_enabled": bool(strtobool(os.getenv("REANA_AUTH_BFF_ENABLED", "true"))),
    # Confidential web client used by the BFF code flow.
    "web_client_id": os.getenv("REANA_AUTH_WEB_CLIENT_ID", "reana-server"),
    "web_client_secret": os.getenv("REANA_AUTH_WEB_CLIENT_SECRET", ""),
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
    # Optional endpoint overrides (resolved from the discovery document
    # when empty, see reana_server.auth.discovery).
    "authorization_url": os.getenv("REANA_AUTH_AUTHORIZATION_URL", ""),
    "token_url": os.getenv("REANA_AUTH_TOKEN_URL", ""),
    "end_session_url": os.getenv("REANA_AUTH_END_SESSION_URL", ""),
    "device_authorization_url": os.getenv(
        "REANA_AUTH_DEVICE_AUTHORIZATION_URL", ""
    ),
    # Automatic linking of a freshly-seen IdP identity to a pre-existing
    # REANA account that has the same verified email (migration aid). It is
    # an account-takeover vector when the issuer does not truly verify
    # emails, so it is DISABLED by default and gated by explicit allow-lists
    # (auth_contract_freeze.md, "User Provisioning Contract"). With an empty
    # allow-list the corresponding check is skipped, so enabling linking
    # without any allow-list trusts every configured issuer/domain.
    "email_linking_enabled": bool(
        strtobool(os.getenv("REANA_AUTH_EMAIL_LINKING_ENABLED", "false"))
    ),
    "email_linking_issuer_allowlist": [
        value.strip()
        for value in os.getenv(
            "REANA_AUTH_EMAIL_LINKING_ISSUER_ALLOWLIST", ""
        ).split(",")
        if value.strip()
    ],
    "email_linking_domain_allowlist": [
        value.strip().lower()
        for value in os.getenv(
            "REANA_AUTH_EMAIL_LINKING_DOMAIN_ALLOWLIST", ""
        ).split(",")
        if value.strip()
    ],
}
"""OIDC/JWT authentication configuration (see AUTH_ARCHITECTURE.md)."""

try:
    REANA_GROUP_BACKENDS = json.loads(os.getenv("REANA_GROUP_BACKENDS", "[]"))
except json.JSONDecodeError:
    logging.error("REANA_GROUP_BACKENDS is not valid JSON, ignoring.")
    REANA_GROUP_BACKENDS = []
"""Group backend configuration.

JSON list of backend definitions, e.g.::

    [{"type": "keycloak", "provider": "keycloak",
      "server_url": "https://auth.reana.example.org", "realm": "reana",
      "groups_claim": "groups", "client_id": "reana-server-internal",
      "client_secret_env": "REANA_GROUP_BACKEND_KEYCLOAK_CLIENT_SECRET"}]

Each backend's service credentials are read from the environment variable
named by ``client_secret_env``. See ``reana_server/groups``.
"""
