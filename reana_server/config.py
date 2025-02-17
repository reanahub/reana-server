# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask application configuration."""

import copy
import json
import os
import re
from typing import Optional

from distutils.util import strtobool
from limits.util import parse
from invenio_app.config import APP_DEFAULT_SECURE_HEADERS
from invenio_oauthclient.contrib import cern_openid
from invenio_oauthclient.contrib.keycloak import KeycloakSettingsHelper
from reana_commons.config import REANA_INFRASTRUCTURE_COMPONENTS_HOSTNAMES
from reana_commons.job_utils import kubernetes_memory_to_bytes

# This database URI import is necessary for Invenio-DB
from reana_db.config import SQLALCHEMY_DATABASE_URI

SQLALCHEMY_TRACK_MODIFICATIONS = False
"""Track modifications flag."""

ADMIN_USER_ID = "00000000-0000-0000-0000-000000000000"

SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/var/reana")

REANA_HOSTNAME = os.getenv("REANA_HOSTNAME", "localhost")

REANA_SSO_CERN_CONSUMER_KEY = os.getenv("CERN_CONSUMER_KEY", "CHANGE_ME")
REANA_SSO_CERN_CONSUMER_SECRET = os.getenv("CERN_CONSUMER_SECRET", "CHANGE_ME")
REANA_SSO_CERN_BASE_URL = os.getenv(
    "CERN_BASE_URL", "https://auth.cern.ch/auth/realms/cern"
)
REANA_SSO_CERN_TOKEN_URL = os.getenv(
    "CERN_TOKEN_URL",
    "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/token",
)
REANA_SSO_CERN_AUTH_URL = os.getenv(
    "CERN_AUTH_URL",
    "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/auth",
)
REANA_SSO_CERN_USERINFO_URL = os.getenv(
    "CERN_USERINFO_URL",
    "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/userinfo",
)

# Load Login Providers Configuration and Secrets as JSON from environment variables
REANA_SSO_LOGIN_PROVIDERS = json.loads(os.getenv("LOGIN_PROVIDERS_CONFIGS", "[]"))
REANA_SSO_LOGIN_PROVIDERS_SECRETS = json.loads(
    os.getenv("LOGIN_PROVIDERS_SECRETS", "{}")
)

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

REANA_KUBERNETES_JOBS_MEMORY_LIMIT = os.getenv("REANA_KUBERNETES_JOBS_MEMORY_LIMIT")
"""Maximum memory limit for user job containers for workflow complexity estimation."""

REANA_KUBERNETES_JOBS_MEMORY_LIMIT_IN_BYTES = (
    kubernetes_memory_to_bytes(REANA_KUBERNETES_JOBS_MEMORY_LIMIT)
    if REANA_KUBERNETES_JOBS_MEMORY_LIMIT
    else 0
)
"""Maximum memory limit for user job containers in bytes."""

REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT = os.getenv(
    "REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT"
)
"""Maximum memory limit that users can assign to their job containers."""

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


# Invenio configuration
# =====================
def _(x):
    """Identity function used to trigger string extraction."""
    return x


# Email configuration
# ===================
#: Email address for support.
SUPPORT_EMAIL = "info@reanahub.io"
#: Disable email sending by default.
MAIL_SUPPRESS_SEND = True

# Accounts
# ========
#: Redis URL
REANA_CACHE_PASSWORD = os.getenv("REANA_CACHE_PASSWORD", "")
ACCOUNTS_SESSION_REDIS_URL = "redis://:{password}@{host}:6379/1".format(
    password=REANA_CACHE_PASSWORD,
    host=REANA_INFRASTRUCTURE_COMPONENTS_HOSTNAMES["cache"],
)
#: Email address used as sender of account registration emails.
SECURITY_EMAIL_SENDER = SUPPORT_EMAIL
#: Email subject for account registration emails.
SECURITY_EMAIL_SUBJECT_REGISTER = _("Welcome to REANA Server!")

#: Enable session/user id request tracing. This feature will add X-Session-ID
#: and X-User-ID headers to HTTP response. You MUST ensure that NGINX (or other
#: proxies) removes these headers again before sending the response to the
#: client. Set to False, in case of doubt.
ACCOUNTS_USERINFO_HEADERS = bool(
    strtobool(os.getenv("ACCOUNTS_USERINFO_HEADERS", "False"))
)
#: Disable password recovery by users.
SECURITY_RECOVERABLE = False
REANA_USER_EMAIL_CONFIRMATION = strtobool(
    os.getenv("REANA_USER_EMAIL_CONFIRMATION", "true")
)
#: Enable user to confirm their email address.
SECURITY_CONFIRMABLE = REANA_USER_EMAIL_CONFIRMATION
if REANA_USER_EMAIL_CONFIRMATION:
    #: Disable user login without confirming their email address.
    SECURITY_LOGIN_WITHOUT_CONFIRMATION = False
    #: Value to be used for the confirmation email link in the API application.
    ACCOUNTS_REST_CONFIRM_EMAIL_ENDPOINT = "/confirm-email"
#: URL endpoint for login.
SECURITY_LOGIN_URL = "/signin"
#: Disable password change by users.
SECURITY_CHANGEABLE = False
#: Modify sign in validaiton error to avoid leaking extra information.
failed_signin_msg = ("Signin failed. Invalid user or password.", "error")
SECURITY_MSG_USER_DOES_NOT_EXIST = failed_signin_msg
SECURITY_MSG_PASSWORD_NOT_SET = failed_signin_msg
SECURITY_MSG_INVALID_PASSWORD = failed_signin_msg
SECURITY_MSG_PASSWORD_INVALID_LENGTH = failed_signin_msg

# CORS
# ====
REST_ENABLE_CORS = True
# change this only while developing
CORS_SEND_WILDCARD = True
CORS_SUPPORTS_CREDENTIALS = False

# Flask configuration
# ===================
# See details on
# http://flask.pocoo.org/docs/0.12/config/#builtin-configuration-values

#: Secret key - each installation (dev, production, ...) needs a separate key.
#: It should be changed before deploying.
SECRET_KEY = os.getenv("REANA_SECRET_KEY", "CHANGE_ME")
"""Secret key used for the application user sessions."""

#: Sets cookie with the secure flag by default
SESSION_COOKIE_SECURE = True
#: Sets session to be samesite to avoid CSRF attacks
SESSION_COOKIE_SAMESITE = "Lax"
#: Since HAProxy and Nginx route all requests no matter the host header
#: provided, the allowed hosts variable is set to localhost. In production it
#: should be set to the correct host and it is strongly recommended to only
#: route correct hosts to the application.

#: In production use the following configuration plus adding  the hostname/ip
#: of the reverse proxy in front of REANA-Server.
if REANA_HOSTNAME:
    APP_ALLOWED_HOSTS = [REANA_HOSTNAME]

# Security configuration
# ======================
PROXYFIX_CONFIG = json.loads(os.getenv("PROXYFIX_CONFIG", '{"x_proto": 1}'))

APP_DEFAULT_SECURE_HEADERS["content_security_policy"] = {}
APP_DEFAULT_SECURE_HEADERS.update(
    json.loads(os.getenv("APP_DEFAULT_SECURE_HEADERS", "{}"))
)
if "REANA_FORCE_HTTPS" in os.environ:
    APP_DEFAULT_SECURE_HEADERS["force_https"] = bool(
        strtobool(os.getenv("REANA_FORCE_HTTPS"))
    )

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


# Note: users that are connecting via reana-client will be treated as guests by the Invenio framework
RATELIMIT_GUEST_USER = _get_rate_limit("REANA_RATELIMIT_GUEST_USER", "20 per second")
RATELIMIT_AUTHENTICATED_USER = _get_rate_limit(
    "REANA_RATELIMIT_AUTHENTICATED_USER", "20 per second"
)
REANA_RATELIMIT_SLOW = _get_rate_limit("REANA_RATELIMIT_SLOW", "1/5 second")

RATELIMIT_PER_ENDPOINT = {
    "launch.launch": REANA_RATELIMIT_SLOW,
}

# Flask-Breadcrumbs needs this variable set
# =========================================
BREADCRUMBS_ROOT = "breadcrumbs"

# Combined OAuth configuration for CERN and generic Keycloak
# ==========================================================

OAUTH_REDIRECT_URL = "/signin_callback"

OAUTHCLIENT_REST_DEFAULT_ERROR_REDIRECT_URL = OAUTH_REDIRECT_URL

OAUTHCLIENT_REMOTE_APPS = dict()
OAUTHCLIENT_REST_REMOTE_APPS = dict()

# Keycloak is only configured if login providers are defined
if REANA_SSO_LOGIN_PROVIDERS:
    # Variables for the first login provider in the JSON
    PROVIDER_NAME = REANA_SSO_LOGIN_PROVIDERS[0]["name"]
    PROVIDER_CONFIG = REANA_SSO_LOGIN_PROVIDERS[0]["config"]
    PROVIDER_SECRETS = REANA_SSO_LOGIN_PROVIDERS_SECRETS[PROVIDER_NAME]

    helper = KeycloakSettingsHelper(
        title=PROVIDER_CONFIG["title"],
        description="",  # This is not used and thus left empty
        base_url=PROVIDER_CONFIG["base_url"],
        realm="",  # The realm_url is set manually below
    )

    KEYCLOAK_APP = copy.deepcopy(helper.remote_app)
    KEYCLOAK_APP["params"]["authorize_url"] = PROVIDER_CONFIG["auth_url"]
    KEYCLOAK_APP["params"]["access_token_url"] = PROVIDER_CONFIG["token_url"]
    KEYCLOAK_APP["params"]["request_token_params"] = {"scope": "openid profile email"}
    KEYCLOAK_APP["authorized_redirect_url"] = OAUTH_REDIRECT_URL
    KEYCLOAK_APP["error_redirect_url"] = OAUTH_REDIRECT_URL

    KEYCLOAK_REST_APP = copy.deepcopy(helper.remote_rest_app)
    KEYCLOAK_REST_APP["params"]["authorize_url"] = PROVIDER_CONFIG["auth_url"]
    KEYCLOAK_REST_APP["params"]["request_token_params"] = {
        "scope": "openid profile email"
    }
    KEYCLOAK_REST_APP["params"]["access_token_url"] = PROVIDER_CONFIG["token_url"]
    KEYCLOAK_REST_APP["authorized_redirect_url"] = OAUTH_REDIRECT_URL
    KEYCLOAK_REST_APP["error_redirect_url"] = OAUTH_REDIRECT_URL

    OAUTHCLIENT_KEYCLOAK_REALM_URL = PROVIDER_CONFIG["realm_url"]
    OAUTHCLIENT_KEYCLOAK_USER_INFO_URL = PROVIDER_CONFIG["userinfo_url"]
    OAUTHCLIENT_KEYCLOAK_VERIFY_EXP = True
    OAUTHCLIENT_KEYCLOAK_VERIFY_AUD = True
    OAUTHCLIENT_KEYCLOAK_AUD = PROVIDER_SECRETS["consumer_key"]

    KEYCLOAK_APP_CREDENTIALS = dict(
        consumer_key=PROVIDER_SECRETS["consumer_key"],
        consumer_secret=PROVIDER_SECRETS["consumer_secret"],
    )

    OAUTHCLIENT_REMOTE_APPS["keycloak"] = KEYCLOAK_APP
    OAUTHCLIENT_REST_REMOTE_APPS["keycloak"] = KEYCLOAK_REST_APP

# CERN SSO configuration
OAUTH_REMOTE_REST_APP = copy.deepcopy(cern_openid.REMOTE_REST_APP)
OAUTH_REMOTE_REST_APP.update(
    {
        "authorized_redirect_url": OAUTH_REDIRECT_URL,
        "error_redirect_url": OAUTH_REDIRECT_URL,
    }
)
OAUTH_REMOTE_REST_APP["params"].update(
    dict(
        request_token_params={"scope": "openid"},
        base_url=REANA_SSO_CERN_BASE_URL,
        access_token_url=REANA_SSO_CERN_TOKEN_URL,
        authorize_url=REANA_SSO_CERN_AUTH_URL,
    )
)
OAUTHCLIENT_CERN_OPENID_USERINFO_URL = REANA_SSO_CERN_USERINFO_URL

CERN_APP_OPENID_CREDENTIALS = dict(
    consumer_key=REANA_SSO_CERN_CONSUMER_KEY,
    consumer_secret=REANA_SSO_CERN_CONSUMER_SECRET,
)

OAUTHCLIENT_REMOTE_APPS["cern_openid"] = OAUTH_REMOTE_REST_APP
OAUTHCLIENT_REST_REMOTE_APPS["cern_openid"] = OAUTH_REMOTE_REST_APP

SECURITY_PASSWORD_SALT = "security-password-salt"

SECURITY_SEND_REGISTER_EMAIL = False

# Gitlab Application configuration
# ================================
REANA_GITLAB_OAUTH_APP_ID = os.getenv("REANA_GITLAB_OAUTH_APP_ID", "CHANGE_ME")
REANA_GITLAB_OAUTH_APP_SECRET = os.getenv("REANA_GITLAB_OAUTH_APP_SECRET", "CHANGE_ME")
REANA_GITLAB_HOST = os.getenv("REANA_GITLAB_HOST", None)
REANA_GITLAB_URL = "https://{}".format((REANA_GITLAB_HOST or "CHANGE ME"))

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
