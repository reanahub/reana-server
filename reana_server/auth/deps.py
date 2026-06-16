# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""FastAPI authentication dependencies.

These are thin native-FastAPI wrappers around the framework-agnostic auth
core (``reana_server.auth.tokens``/``provision``). They replace the Flask
``signin_required`` decorator (``reana_server/decorators.py``) while reusing
the exact same validation, role gate and JIT-provisioning logic.

Design (see ``AUTH_ARCHITECTURE.md`` rev 3 / ``FASTAPI_MIGRATION_PLAN.md``):

* **Roles travel in the JWT** and are enforced declaratively via FastAPI
  ``SecurityScopes``: a route asks for ``Security(get_current_user,
  scopes=["reana:user"])`` to require the role, or ``scopes=[]`` to allow any
  authenticated identity (the role-optional endpoints ``/you``, ``/info`` …).
* **Groups live in the REANA DB** (never in the token); per-workflow group
  authorization is a separate plain dependency querying
  ``reana_db.utils.user_can_read_workflow`` — *not* a token scope.

MVP scope: the ``Authorization: Bearer`` path (the credential the CLI and the
VRE use). The BFF cookie + CSRF + transparent-refresh path and the GitLab
webhook-secret path (both present in ``decorators.py``) are deliberately not
wired here yet; they slot in at the marked place without changing callers.
"""

from typing import Optional

from fastapi import HTTPException, Request, Response, Security, status
from fastapi.security import OAuth2AuthorizationCodeBearer, SecurityScopes
from reana_db.models import User

from reana_server.auth import (
    AuthError,
    InvalidTokenError,
    MissingRoleError,
    ProvisioningError,
    get_or_provision_user,
    require_role,
    validate_access_token,
)
from reana_server.auth.tokens import extract_roles
from reana_server.auth.sessions import (
    AUTH_COOKIE,
    csrf_ok,
    decode_expired_token,
    refresh_session,
    set_auth_cookies,
)
from reana_server.config import REANA_AUTH

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# The OAuth2 scheme is what makes the generated OpenAPI advertise the issuer's
# authorization-code + PKCE flow (and renders the Swagger "Authorize" button).
# The URLs only matter for that interactive UI; the scheme's runtime job here
# is to pull the bearer token out of the ``Authorization`` header. ``auto_error``
# is off so we can emit our own 401 body (and, later, fall back to the cookie).
oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl=REANA_AUTH.get("authorization_url") or "/oauth2/authorize",
    tokenUrl=REANA_AUTH.get("token_url") or "/oauth2/token",
    refreshUrl=REANA_AUTH.get("token_url") or None,
    scopes={
        "reana:user": "Permission to use REANA",
        "reana:admin": "Administrative access",
    },
    auto_error=False,
)


def _challenge(security_scopes: SecurityScopes) -> str:
    """Build the ``WWW-Authenticate`` header value for a 401."""
    if security_scopes.scopes:
        return f'Bearer scope="{" ".join(security_scopes.scopes)}"'
    return "Bearer"


async def get_current_user(
    security_scopes: SecurityScopes,
    request: Request,
    response: Response,
    token: Optional[str] = Security(oauth2_scheme),
) -> User:
    """Resolve the request's bearer token to a REANA :class:`User`.

    The route's required scopes drive the role gate: when the configured
    required role (``reana:user`` by default) is among the requested scopes,
    it is enforced on the token; otherwise any valid identity of the trusted
    issuer is accepted (first-time identities are still role-gated inside
    provisioning, mirroring ``decorators.py``).

    Error mapping matches the Flask decorator: invalid/expired/wrong-issuer
    token → 401; missing role or provisioning failure → 403.
    """
    raw_token = token
    via_cookie = False
    if not raw_token:
        # BFF browser session: the access JWT travels in an httpOnly cookie.
        cookie_token = request.cookies.get(AUTH_COOKIE)
        if cookie_token and REANA_AUTH["bff_enabled"]:
            via_cookie = True
            if request.method not in _SAFE_METHODS and not csrf_ok(
                request.headers, request.cookies
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="CSRF token missing or invalid.",
                )
            raw_token = cookie_token
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "User not signed in. Authenticate with a Bearer token "
                    "(see `reana-client login`) or via the web login."
                ),
                headers={"WWW-Authenticate": _challenge(security_scopes)},
            )

    refreshed_token = None
    try:
        try:
            claims = validate_access_token(raw_token)
        except InvalidTokenError:
            # A bearer token that fails is rejected; an expired *cookie*
            # token is transparently refreshed via the server-side session.
            if not via_cookie:
                raise
            expired_claims = decode_expired_token(raw_token)
            refreshed_token = refresh_session(
                expired_claims.get("sid") or expired_claims["sub"]
            )
            if not refreshed_token:
                raise InvalidTokenError("Session expired, please log in again.")
            raw_token = refreshed_token
            claims = validate_access_token(raw_token)
        if REANA_AUTH["required_role"] in security_scopes.scopes:
            require_role(claims)
        user = get_or_provision_user(claims, raw_token)
        # Expose the token roles (and claims) to endpoints that need them,
        # e.g. ``/api/you`` reports the caller's roles without a second
        # token parse and without putting roles on the User row.
        request.state.reana_roles = extract_roles(claims)
        request.state.reana_claims = claims
    except InvalidTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
            headers={"WWW-Authenticate": _challenge(security_scopes)},
        )
    except (MissingRoleError, ProvisioningError, AuthError) as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(error)
        )
    if refreshed_token:
        set_auth_cookies(
            response, refreshed_token, existing_cookies=request.cookies
        )
    return user
