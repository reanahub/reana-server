# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask application configuration."""

import copy
import os
from datetime import timedelta

from invenio_app.config import APP_DEFAULT_SECURE_HEADERS
from invenio_oauthclient.contrib import cern

# Database
# ========
#: Database URI including user and password
from reana_db.config import SQLALCHEMY_DATABASE_URI

AVAILABLE_WORKFLOW_ENGINES = [
    'yadage',
    'cwl',
    'serial'
]
"""Available workflow engines."""

ADMIN_USER_ID = "00000000-0000-0000-0000-000000000000"

SHARED_VOLUME_PATH = os.getenv('SHARED_VOLUME_PATH', '/var/reana')


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
#: Email address used as sender of account registration emails.
SECURITY_EMAIL_SENDER = SUPPORT_EMAIL
#: Email subject for account registration emails.
SECURITY_EMAIL_SUBJECT_REGISTER = _(
    "Welcome to REANA Server!")

#: Enable session/user id request tracing. This feature will add X-Session-ID
#: and X-User-ID headers to HTTP response. You MUST ensure that NGINX (or other
#: proxies) removes these headers again before sending the response to the
#: client. Set to False, in case of doubt.
ACCOUNTS_USERINFO_HEADERS = True


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
SECRET_KEY = 'CHANGE_ME'
#: Max upload size for form data via application/mulitpart-formdata.
MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100 MiB
#: Sets cookie with the secure flag by default
SESSION_COOKIE_SECURE = True
#: Since HAProxy and Nginx route all requests no matter the host header
#: provided, the allowed hosts variable is set to localhost. In production it
#: should be set to the correct host and it is strongly recommended to only
#: route correct hosts to the application.
APP_ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'reana.cern.ch']

# Security configuration
# ======================
APP_DEFAULT_SECURE_HEADERS["content_security_policy"] = {}
APP_DEFAULT_SECURE_HEADERS["force_https"] = True
APP_DEFAULT_SECURE_HEADERS["frame_options"] = "allowfrom"
APP_DEFAULT_SECURE_HEADERS["frame_options_allow_from"] = "*"


# Flask-Breadcrumbs needs this variable set
# =========================================
BREADCRUMBS_ROOT = 'breadcrumbs'

CERN_REMOTE_APP = copy.deepcopy(cern.REMOTE_APP)

OAUTHCLIENT_REMOTE_APPS = dict(
    cern=CERN_REMOTE_APP,
)

CERN_APP_CREDENTIALS = dict(
    consumer_key='CHANGE_ME',
    consumer_secret='CHANGE_ME',
)

DEBUG = True

SECURITY_PASSWORD_SALT = 'security-password-salt'

SECURITY_SEND_REGISTER_EMAIL = False
