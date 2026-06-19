# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Liveness endpoint (unauthenticated)."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["ping"])


class PingResponse(BaseModel):
    """Liveness payload, matching the legacy ``/ping`` shape."""

    status: str
    message: str


@router.get("/ping", response_model=PingResponse, summary="Liveness probe")
async def ping() -> PingResponse:
    """Return a static OK payload; no authentication required."""
    return PingResponse(status="200", message="OK")
