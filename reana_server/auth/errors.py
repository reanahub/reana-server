# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server authentication errors."""


class AuthError(Exception):
    """Base class for authentication/authorization errors."""


class InvalidTokenError(AuthError):
    """The presented token could not be validated (malformed, bad signature,
    wrong issuer/audience, expired).

    Maps to HTTP 401.
    """


class MissingRoleError(AuthError):
    """The token is valid but the user lacks the required REANA role.

    Maps to HTTP 403.
    """


class ProvisioningError(AuthError):
    """The user could not be provisioned or linked from IdP data."""
