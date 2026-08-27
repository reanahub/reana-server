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


class IssuerUnavailableError(AuthError):
    """The configured identity provider is temporarily unavailable.

    Maps to HTTP 502/503 rather than an authentication or entitlement error.
    """


class IssuerKeyUnavailableError(IssuerUnavailableError):
    """The issuer cannot currently supply usable key material.

    This is distinct from a definitive token-validation failure: malformed or
    empty issuer JWKS and a key rotation that cannot currently be followed may
    recover without the browser discarding its refresh-capable BFF session.
    """


class UnknownKeyHealthyBackoffError(IssuerKeyUnavailableError):
    """An unseen key id arrived during a healthy unknown-kid refresh backoff.

    Raised when the cache is otherwise healthy (the last refresh attempt
    succeeded). A subclass, not a new raise condition: any caller that does not
    explicitly handle it keeps today's IssuerKeyUnavailableError/503
    treatment. Only the bearer-token path distinguishes it, mapping it to
    401 instead -- a one-shot API call gains nothing from a 503 that just
    means "ask again in a few seconds," whereas the cookie/BFF path stays
    conservative because a genuine key rotation arriving during this same
    window is more consequential to misclassify there (a real user's
    session, not a single request). Never raised when the last refresh
    itself failed (JWKSCache._refresh_failed_at) -- that case stays a
    plain IssuerKeyUnavailableError.
    """


class IssuerMisconfiguredError(AuthError):
    """The issuer/discovery-document configuration is invalid or incomplete.

    Unlike :class:`IssuerUnavailableError`, retrying will not help -- an
    administrator must fix the configuration (e.g. an explicit endpoint
    override that fails validation, or a discovery document that doesn't
    advertise an endpoint REANA requires). Maps to HTTP 500, not 403/503.
    """


class InvalidTokenError(AuthError):
    """The presented token could not be validated.

    This includes malformed tokens, bad signatures, wrong issuer or audience,
    and expired tokens.

    Maps to HTTP 401.
    """


class MissingRoleError(AuthError):
    """The token is valid but the user lacks the required REANA role.

    Maps to HTTP 403.
    """


class ProvisioningError(AuthError):
    """The user could not be provisioned or linked from IdP data."""


class SessionUnavailableError(AuthError):
    """The browser-session backend is temporarily unavailable.

    Maps to HTTP 503 without invalidating the browser's session cookies.
    """
