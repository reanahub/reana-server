# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Cluster health status endpoint (authenticated, role-optional)."""

import logging
import traceback

from fastapi import APIRouter, Security
from fastapi.responses import JSONResponse
from reana_db.models import User

from reana_server.auth.deps import get_current_user
from reana_server.status import ClusterHealth, ClusterHealthSchema

router = APIRouter(tags=["status"])


@router.get("/status", summary="Cluster health status")
def status(user: User = Security(get_current_user, scopes=[])):
    """Return node/job/workflow/session health (role-optional)."""
    try:
        return ClusterHealthSchema().dump(ClusterHealth())
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)
