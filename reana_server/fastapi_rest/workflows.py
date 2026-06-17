# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Workflow endpoints (reads via the workflow-controller).

Ported from the Flask blueprint, keeping the (synchronous) bravado
workflow-controller client — FastAPI runs ``def`` handlers in a threadpool,
so the blocking call is fine. The proxied client is a ``LocalProxy`` over a
plain callable, so it needs no Flask app context.
"""

import logging
import traceback
from typing import List, Optional

from bravado.exception import HTTPError
from fastapi import APIRouter, Query, Security
from fastapi.responses import JSONResponse
from reana_db.models import User

from reana_db.utils import _get_workflow_with_uuid_or_name

from reana_server.api_client import current_rwc_api_client
from reana_server.auth.deps import get_current_user

router = APIRouter(tags=["workflows"])

# Per-workflow reads require the REANA role (the controller enforces
# owner/share access on top via the user uuid / DB criterion).
_RoleUser = Security(get_current_user, scopes=["reana:user"])


def _rwc_error(error: Exception) -> JSONResponse:
    """Translate a workflow-controller/bravado error to a JSON response."""
    if isinstance(error, HTTPError):
        return JSONResponse(
            content=error.response.json(), status_code=error.response.status_code
        )
    if isinstance(error, ValueError):
        return JSONResponse(content={"message": str(error)}, status_code=403)
    logging.error(traceback.format_exc())
    return JSONResponse(content={"message": str(error)}, status_code=500)


@router.get("/workflows", summary="List workflows")
def get_workflows(
    type: str = Query("batch"),
    verbose: bool = Query(False),
    search: Optional[str] = Query(None),
    sort: str = Query("desc"),
    status: Optional[List[str]] = Query(None),
    page: Optional[int] = Query(None, ge=1),
    size: Optional[int] = Query(None, ge=1),
    include_progress: Optional[bool] = Query(None),
    include_workspace_size: Optional[bool] = Query(None),
    workflow_id_or_name: Optional[str] = Query(None),
    shared: Optional[bool] = Query(None),
    shared_by: Optional[str] = Query(None),
    shared_with: Optional[str] = Query(None),
    # Role-optional like the legacy endpoint: the controller scopes results to
    # the user, and the UI can show an "access not granted" state.
    user: User = Security(get_current_user, scopes=[]),
):
    """Return the caller's workflows (and, with ``shared``, shared ones)."""
    optional = {
        "page": page,
        "size": size,
        "include_progress": include_progress,
        "include_workspace_size": include_workspace_size,
        "workflow_id_or_name": workflow_id_or_name,
        "shared": shared,
        "shared_by": shared_by,
        "shared_with": shared_with,
    }
    kwargs = {key: value for key, value in optional.items() if value is not None}
    try:
        response, http_response = current_rwc_api_client.api.get_workflows(
            user=str(user.id_),
            type=type,
            search=search,
            sort=sort,
            status=status or None,
            verbose=bool(verbose),
            **kwargs,
        ).result()
        return JSONResponse(
            content=response, status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001 - mapped to a JSON response
        return _rwc_error(error)


@router.get("/workflows/{workflow_id_or_name}/status", summary="Workflow status")
def get_workflow_status(workflow_id_or_name: str, user: User = _RoleUser):
    """Return a workflow's status (controller proxy)."""
    try:
        response, http_response = current_rwc_api_client.api.get_workflow_status(
            user=str(user.id_), workflow_id_or_name=workflow_id_or_name
        ).result()
        return JSONResponse(
            content=response, status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.get(
    "/workflows/{workflow_id_or_name}/parameters", summary="Workflow parameters"
)
def get_workflow_parameters(workflow_id_or_name: str, user: User = _RoleUser):
    """Return a workflow's input parameters (controller proxy)."""
    try:
        response, http_response = (
            current_rwc_api_client.api.get_workflow_parameters(
                user=str(user.id_), workflow_id_or_name=workflow_id_or_name
            ).result()
        )
        return JSONResponse(
            content=response, status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.get("/workflows/{workflow_id_or_name}/logs", summary="Workflow logs")
def get_workflow_logs(
    workflow_id_or_name: str,
    page: Optional[int] = Query(None, ge=1),
    size: Optional[int] = Query(None, ge=1),
    user: User = _RoleUser,
):
    """Return a workflow's logs (controller proxy)."""
    params = {k: v for k, v in {"page": page, "size": size}.items() if v is not None}
    try:
        response, http_response = current_rwc_api_client.api.get_workflow_logs(
            user=str(user.id_), workflow_id_or_name=workflow_id_or_name, **params
        ).result()
        return JSONResponse(
            content=response, status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.get(
    "/workflows/{workflow_id_or_name}/workspace", summary="List workspace files"
)
def get_files(
    workflow_id_or_name: str,
    file_name: Optional[str] = Query(None),
    page: Optional[int] = Query(None, ge=1),
    size: Optional[int] = Query(None, ge=1),
    search: Optional[str] = Query(None),
    user: User = _RoleUser,
):
    """List a workflow's workspace files (controller proxy)."""
    params = {
        k: v
        for k, v in {
            "file_name": file_name,
            "page": page,
            "size": size,
            "search": search,
        }.items()
        if v is not None
    }
    try:
        response, http_response = current_rwc_api_client.api.get_files(
            user=str(user.id_), workflow_id_or_name=workflow_id_or_name, **params
        ).result()
        return JSONResponse(
            content=http_response.json(), status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.get(
    "/workflows/{workflow_id_or_name}/specification",
    summary="Workflow specification",
)
def get_workflow_specification(
    workflow_id_or_name: str, user: User = _RoleUser
):
    """Return a workflow's REANA specification + parameters (DB, group-aware)."""
    try:
        workflow = _get_workflow_with_uuid_or_name(
            workflow_id_or_name, str(user.id_), True
        )
        return {
            "specification": workflow.reana_specification,
            "parameters": workflow.input_parameters or {},
        }
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)
