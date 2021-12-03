# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask application configuration."""

import copy
import os
import re

from distutils.util import strtobool
from invenio_app.config import APP_DEFAULT_SECURE_HEADERS
from invenio_oauthclient.contrib import cern
from reana_commons.config import REANA_INFRASTRUCTURE_COMPONENTS_HOSTNAMES

# This database URI import is necessary for Invenio-DB
from reana_db.config import SQLALCHEMY_DATABASE_URI

SQLALCHEMY_TRACK_MODIFICATIONS = False
"""Track modifications flag."""

ADMIN_USER_ID = "00000000-0000-0000-0000-000000000000"

SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/var/reana")

REANA_HOSTNAME = os.getenv("REANA_HOSTNAME")

REANA_SSO_CERN_CONSUMER_KEY = os.getenv("CERN_CONSUMER_KEY", "CHANGE_ME")

REANA_SSO_CERN_CONSUMER_SECRET = os.getenv("CERN_CONSUMER_SECRET", "CHANGE_ME")

REANA_COMPLEXITY_JOBS_MEMORY_LIMIT = os.getenv("REANA_KUBERNETES_JOBS_MEMORY_LIMIT")
"""Maximum memory limit for user job containers for workflow complexity estimation."""

REANA_WORKFLOW_SCHEDULING_POLICY = os.getenv("REANA_WORKFLOW_SCHEDULING_POLICY", "fifo")

REANA_WORKFLOW_SCHEDULING_POLICIES = ["fifo", "balanced"]
"""REANA workflow scheduling policies.
- ``fifo``: first-in first-out strategy starting workflows as they come.
- ``balanced``: a weighted strategy taking into account existing multi-user workloads and the DAG complexity of incoming workflows.
"""


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
ACCOUNTS_SESSION_REDIS_URL = "redis://{host}:6379/1".format(
    host=REANA_INFRASTRUCTURE_COMPONENTS_HOSTNAMES["cache"]
)
#: Email address used as sender of account registration emails.
SECURITY_EMAIL_SENDER = SUPPORT_EMAIL
#: Email subject for account registration emails.
SECURITY_EMAIL_SUBJECT_REGISTER = _("Welcome to REANA Server!")

#: Enable session/user id request tracing. This feature will add X-Session-ID
#: and X-User-ID headers to HTTP response. You MUST ensure that NGINX (or other
#: proxies) removes these headers again before sending the response to the
#: client. Set to False, in case of doubt.
ACCOUNTS_USERINFO_HEADERS = True
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
SECRET_KEY = "CHANGE_ME"
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
PROXYFIX_CONFIG = {"x_proto": 1}
APP_DEFAULT_SECURE_HEADERS["content_security_policy"] = {}
APP_HEALTH_BLUEPRINT_ENABLED = False

# Rate limiting configuration using invenio-app
# ===========================


def _is_valid_rate_limit(rate_limit: str) -> bool:
    return bool(
        re.match(r"[0-9]+(\sper\s|\/)(second|minute|hour|day|month|year)", rate_limit)
    )


def _get_rate_limit(env_variable: str, default: str) -> str:
    env_value = os.getenv(env_variable, "")
    return env_value if _is_valid_rate_limit(env_value) else default


# Note: users that are connecting via reana-client will be treated as guests by the Invenio framework
RATELIMIT_GUEST_USER = _get_rate_limit("REANA_RATELIMIT_GUEST_USER", "20 per second")
RATELIMIT_AUTHENTICATED_USER = _get_rate_limit(
    "REANA_RATELIMIT_AUTHENTICATED_USER", "20 per second"
)

# Flask-Breadcrumbs needs this variable set
# =========================================
BREADCRUMBS_ROOT = "breadcrumbs"

CERN_REMOTE_APP = copy.deepcopy(cern.REMOTE_APP)

OAUTHCLIENT_REMOTE_APPS = dict(cern=CERN_REMOTE_APP,)

REANA_CERN_ALLOW_SOCIAL_LOGIN = os.getenv("REANA_CERN_ALLOW_SOCIAL_LOGIN", False)

if REANA_CERN_ALLOW_SOCIAL_LOGIN:
    OAUTHCLIENT_CERN_ALLOWED_IDENTITY_CLASSES = (
        cern.OAUTHCLIENT_CERN_ALLOWED_IDENTITY_CLASSES + ["Unverified External"]
    )

CERN_APP_CREDENTIALS = dict(
    consumer_key=REANA_SSO_CERN_CONSUMER_KEY,
    consumer_secret=REANA_SSO_CERN_CONSUMER_SECRET,
)

DEBUG = True

SECURITY_PASSWORD_SALT = "security-password-salt"

SECURITY_SEND_REGISTER_EMAIL = False

# Gitlab Application configuration
# ================================
REANA_GITLAB_OAUTH_APP_ID = os.getenv("REANA_GITLAB_OAUTH_APP_ID", "CHANGE_ME")
REANA_GITLAB_OAUTH_APP_SECRET = os.getenv("REANA_GITLAB_OAUTH_APP_SECRET", "CHANGE_ME")
REANA_GITLAB_URL = "https://{}".format(os.getenv("REANA_GITLAB_HOST", "CHANGE_ME"))


# Email configuration
# ===================
ADMIN_EMAIL = os.getenv("REANA_EMAIL_SENDER", "CHANGE_ME")


# Workflow scheduler
# ==================
REANA_SCHEDULER_REQUEUE_SLEEP = float(os.getenv("REANA_SCHEDULER_REQUEUE_SLEEP", "15"))
"""How many seconds to wait between consuming workflows."""
