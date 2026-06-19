# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Userinfo endpoint access.

Group memberships and profile attributes are deliberately read from the
issuer's userinfo endpoint instead of access-token claims: userinfo travels
in the HTTP body and therefore has no cookie/header size limits (see
AUTH_ARCHITECTURE.md §6). This is called once per identity lifetime (JIT
provisioning), at logins, and by the periodic group refresh — never on the
per-request hot path.
"""

import requests

from reana_server.config import REANA_AUTH
from reana_server.auth.discovery import get_endpoint
from reana_server.auth.errors import AuthError


def fetch_userinfo(token):
    """Fetch the userinfo document for the presented access token.

    :param token: the raw bearer access token.
    :raises AuthError: on transport errors or a userinfo response without
        an email (REANA requires emails for display and share-by-email).
    """
    userinfo_url = get_endpoint("userinfo_url")
    try:
        response = requests.get(
            userinfo_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REANA_AUTH["http_timeout"],
        )
        response.raise_for_status()
        userinfo = response.json()
    except (requests.RequestException, ValueError) as error:
        raise AuthError(f"Could not fetch userinfo from issuer: {error}")
    if not userinfo.get("email"):
        raise AuthError("Userinfo response from issuer is missing 'email'.")
    return userinfo
