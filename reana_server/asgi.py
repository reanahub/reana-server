# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""ASGI entry point: the FastAPI application for REANA-Server.

This is the FastAPI counterpart of the Flask ``reana_server/factory.py``.
MVP: it wires the native JWT auth dependencies and an initial slice of
routers, and auto-generates the OpenAPI document (served at
``/api/openapi.json``) with the issuer's OAuth2 authorization-code + PKCE
flow advertised, so the Swagger "Authorize" button drives the real login.

Run with::

    uvicorn reana_server.asgi:app
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from reana_server.auth.errors import AuthError, InvalidTokenError
from reana_server.config import REANA_AUTH, REANA_URL
from reana_server.rest import (
    auth,
    config,
    gitlab,
    groups,
    info,
    launch,
    ping,
    quota,
    secrets,
    status,
    users,
    workflows,
)
from reana_server.version import __version__


#: HTTP response security headers, mirroring the hardening that PR #766
#: ("restrict CORS and add HTTP security headers") added to the Flask app
#: before it was retired. The Content-Security-Policy must be kept in sync
#: with the reana-ui ``nginx/reana-ui.conf`` content security policy.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "base-uri 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Permissions-Policy": (
        "accelerometer=(), ambient-light-sensor=(), camera=(), "
        "display-capture=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
}


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="REANA Server",
        description="REANA REST API — FastAPI + JWT (Bearer) authentication.",
        version=__version__,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        # Pre-fill the Swagger "Authorize" dialog with the public CLI client
        # and force PKCE, so interactive auth uses the real issuer flow.
        swagger_ui_init_oauth={
            "clientId": REANA_AUTH["cli_client_id"],
            "usePkceWithAuthorizationCodeGrant": True,
            "scopes": "openid profile email reana:user",
        },
    )

    # Restrict CORS to the REANA hostname instead of a wildcard, so other
    # websites cannot read API responses from the browser (parity with #766).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[REANA_URL],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _add_security_headers(request, call_next):
        """Set HTTP security headers on every response (parity with #766)."""
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        return response

    @app.exception_handler(AuthError)
    async def _auth_error_handler(request, exc: AuthError) -> JSONResponse:
        """Defense in depth: any AuthError escaping a dependency → JSON.

        The dependencies already translate to ``HTTPException``; this keeps
        a uniform ``{"message": ...}`` body if one is ever raised elsewhere.
        """
        status_code = 401 if isinstance(exc, InvalidTokenError) else 403
        return JSONResponse(status_code=status_code, content={"message": str(exc)})

    app.include_router(ping.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(config.router, prefix="/api")
    app.include_router(gitlab.router, prefix="/api")
    app.include_router(info.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    app.include_router(groups.router, prefix="/api")
    app.include_router(status.router, prefix="/api")
    app.include_router(secrets.router, prefix="/api")
    app.include_router(launch.router, prefix="/api")
    app.include_router(quota.router, prefix="/api")
    app.include_router(workflows.router, prefix="/api")

    return app


app = create_app()
