# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Access to authentication configuration and per-application state."""

import requests
from flask import current_app


def get_auth_config():
    """Return the current Flask application's authentication configuration.

    Authentication code is always executed in an application context.  Keeping
    the lookup here prevents imported module-level configuration from diverging
    from application-factory overrides.
    """
    return current_app.config["REANA_AUTH"]


def get_issuer_request_kwargs():
    """Return the common security policy for outbound OIDC HTTP requests."""
    auth_config = get_auth_config()
    return {
        "timeout": auth_config["http_timeout"],
        "allow_redirects": False,
        "verify": auth_config.get("ca_bundle") or True,
    }


def raise_for_issuer_status(response):
    """Reject redirects as well as normal HTTP error responses."""
    status_code = response.status_code
    if isinstance(status_code, int) and 300 <= status_code < 400:
        raise requests.HTTPError(
            f"OIDC issuer redirected the request (HTTP {status_code}).",
            response=response,
        )
    response.raise_for_status()
