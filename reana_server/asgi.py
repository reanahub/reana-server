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
from reana_server.config import REANA_AUTH
from reana_server.fastapi_rest import (
    auth,
    config,
    groups,
    info,
    launch,
    ping,
    secrets,
    status,
    users,
    workflows,
)
from reana_server.version import __version__


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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
    app.include_router(info.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    app.include_router(groups.router, prefix="/api")
    app.include_router(status.router, prefix="/api")
    app.include_router(secrets.router, prefix="/api")
    app.include_router(launch.router, prefix="/api")
    app.include_router(workflows.router, prefix="/api")

    return app


app = create_app()
