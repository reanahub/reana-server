# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Userinfo endpoint access.

Profile attributes and optional role claims are read from the issuer's
UserInfo endpoint during provisioning and login.
"""

import requests

from reana_server.auth.config import get_issuer_request_kwargs, raise_for_issuer_status
from reana_server.auth.discovery import get_endpoint
from reana_server.auth.errors import IssuerUnavailableError, ProvisioningError


def fetch_userinfo(token):
    """Fetch the userinfo document for the presented access token.

    :param token: the raw bearer access token.
    :raises IssuerUnavailableError: on transport, HTTP or decoding failures, so
        an issuer outage during first-login provisioning is reported as a 502/503
        availability error rather than an authorization denial.
    :raises ProvisioningError: when the profile is unusable (no email); REANA
        requires emails for display and share-by-email.
    """
    userinfo_url = get_endpoint("userinfo_url")
    try:
        response = requests.get(
            userinfo_url,
            headers={"Authorization": f"Bearer {token}"},
            **get_issuer_request_kwargs(),
        )
        raise_for_issuer_status(response)
        userinfo = response.json()
    except (requests.RequestException, ValueError) as error:
        raise IssuerUnavailableError(
            f"Could not fetch userinfo from issuer: {error}"
        ) from error
    if not userinfo.get("email"):
        raise ProvisioningError("Userinfo response from issuer is missing 'email'.")
    return userinfo
