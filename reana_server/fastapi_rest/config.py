# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""UI configuration endpoint (public)."""

from fastapi import APIRouter
from reana_commons.config import REANAConfig

from reana_server.config import REANA_AUTH

router = APIRouter(tags=["config"])


@router.get("/config", summary="UI configuration (public)")
async def get_config() -> dict:
    """Return the configuration the web UI needs to bootstrap.

    Public: the UI fetches this before authenticating to learn the login
    endpoints and feature flags.
    """
    ui_config = dict(REANAConfig.load("ui") or {})
    # Auth discovery for the web UI (AUTH_ARCHITECTURE.md §5.1).
    ui_config["auth"] = {
        "bff_enabled": bool(REANA_AUTH["bff_enabled"] and REANA_AUTH["issuer"]),
        "login_url": "/api/login",
        "logout_url": "/api/logout",
    }
    return ui_config
