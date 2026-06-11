# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server OIDC/JWT authentication.

This package implements the resource-server side of REANA's JWT
authentication (see ``AUTH_ARCHITECTURE.md``): stateless validation of
access tokens issued by the configured trusted OIDC issuer (the bundled
Keycloak by default, any OIDC-compliant issuer otherwise), and just-in-time
provisioning of REANA users from the issuer's userinfo endpoint.
"""

from reana_server.auth.errors import (
    AuthError,
    InvalidTokenError,
    MissingRoleError,
    ProvisioningError,
)
from reana_server.auth.tokens import require_role, validate_access_token
from reana_server.auth.provision import get_or_provision_user

__all__ = (
    "AuthError",
    "InvalidTokenError",
    "MissingRoleError",
    "ProvisioningError",
    "get_or_provision_user",
    "require_role",
    "validate_access_token",
)
