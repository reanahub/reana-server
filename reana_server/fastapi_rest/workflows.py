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
import os
import traceback
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bravado.exception import HTTPError
from fastapi import APIRouter, Body, Query, Request, Security
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from jsonschema.exceptions import ValidationError
from starlette.concurrency import run_in_threadpool
from reana_commons import workspace
from reana_commons.config import REANA_WORKFLOW_ENGINES
from reana_commons.errors import REANAQuotaExceededError, REANAValidationError
from reana_commons.specification import load_reana_spec
from reana_commons.validation.operational_options import (
    validate_operational_options,
)
from reana_commons.validation.utils import validate_workflow_name
from reana_db.database import Session
from reana_db.models import InteractiveSessionType, RunStatus, User
from reana_db.utils import _get_workflow_with_uuid_or_name

from reana_server.api_client import current_rwc_api_client
from reana_server.auth.deps import get_current_user
from reana_server.config import REANA_HOSTNAME
from reana_server.deleter import Deleter, InOrOut
from reana_server.gitlab_client import (
    GitLabClientInvalidToken,
    GitLabClientRequestError,
)
from reana_server.groups.shares import (
    GroupBackendUnavailableError,
    GroupNotFoundError,
    GroupShareConflictError,
    GroupShareValidationError,
    get_group_shares_for_workflow,
    parse_valid_until,
    share_workflow_with_group,
    unshare_workflow_with_group,
)
from reana_server.rest.workflows import _start_workflow  # shared queue-publish logic
from reana_server.utils import (
    _fail_gitlab_commit_build_status,
    _get_reana_yaml_from_gitlab,
    _load_and_save_yadage_spec,
    get_quota_excess_message,
    get_workspace_retention_rules,
    is_uuid_v4,
    prevent_disk_quota_excess,
    publish_workflow_submission,
)
from reana_server.validation import (
    validate_dask_limits,
    validate_images,
    validate_inputs,
    validate_workspace_path,
)

router = APIRouter(tags=["workflows"])

# Per-workflow reads require the REANA role (the controller enforces
# owner/share access on top via the user uuid / DB criterion).
_RoleUser = Security(get_current_user, scopes=["reana:user"])


def _rwc_error(error: Exception) -> JSONResponse:
    """Translate a workflow-controller/bravado error to a JSON response."""
    if isinstance(error, HTTPError):
        try:
            content = error.response.json()
        except Exception:  # noqa: BLE001 - controller returned a non-JSON body
            content = {"message": str(error)}
        return JSONResponse(content=content, status_code=error.response.status_code)
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


@router.post("/workflows", summary="Create a workflow")
def create_workflow(
    payload: dict = Body(...),
    workflow_name: str = Query(""),
    user: User = _RoleUser,
):
    """Create a workflow from a REANA specification (or a GitLab webhook)."""
    try:
        request_from_gitlab = "object_kind" in payload
        if request_from_gitlab:
            (
                reana_spec_file,
                git_url,
                wf_name,
                git_branch,
                git_commit_sha,
            ) = _get_reana_yaml_from_gitlab(payload, user.id_)
            git_data = {
                "git_url": git_url,
                "git_branch": git_branch,
                "git_commit_sha": git_commit_sha,
            }
        else:
            git_data = {}
            reana_spec_file = payload
            wf_name = workflow_name

        if user.has_exceeded_quota() and request_from_gitlab:
            message = f"User quota exceeded. Please check {REANA_HOSTNAME}"
            _fail_gitlab_commit_build_status(
                user, git_url, git_commit_sha, message
            )
            return JSONResponse({"message": "Gitlab webhook was processed"}, 200)
        elif user.has_exceeded_quota():
            raise REANAQuotaExceededError(get_quota_excess_message(user))

        validate_workflow_name(wf_name)
        if is_uuid_v4(wf_name):
            return JSONResponse(
                {"message": "Workflow name cannot be a valid UUIDv4."}, 400
            )
        workflow_engine = reana_spec_file["workflow"]["type"]
        if workflow_engine not in REANA_WORKFLOW_ENGINES:
            raise Exception("Unknown workflow type.")
        operational_options = validate_operational_options(
            workflow_engine, reana_spec_file.get("inputs", {}).get("options", {})
        )
        workspace_root_path = reana_spec_file.get("workspace", {}).get("root_path")
        validate_workspace_path(reana_spec_file)
        validate_inputs(reana_spec_file)
        validate_images(reana_spec_file)
        validate_dask_limits(reana_spec_file)
        retention_rules = get_workspace_retention_rules(
            reana_spec_file.get("workspace", {}).get("retention_days")
        )
        workflow_dict = {
            "reana_specification": reana_spec_file,
            "workflow_name": wf_name,
            "operational_options": operational_options,
            "retention_rules": retention_rules,
        }
        if git_data:
            workflow_dict["git_data"] = git_data
        response, http_response = current_rwc_api_client.api.create_workflow(
            workflow=workflow_dict,
            user=str(user.id_),
            workspace_root_path=workspace_root_path,
        ).result()

        if git_data:
            workflow = _get_workflow_with_uuid_or_name(
                response["workflow_id"], str(user.id_)
            )
            if workflow.type_ == "yadage":
                _load_and_save_yadage_spec(workflow, operational_options)
            elif workflow.type_ in ["cwl", "snakemake"]:
                reana_yaml_path = os.path.join(
                    workflow.workspace_path, "reana.yaml"
                )
                workflow.reana_specification = load_reana_spec(
                    reana_yaml_path, workflow.workspace_path
                )
                Session.commit()
            validate_images(workflow.reana_specification)
            publish_workflow_submission(workflow, user.id_, payload)
        return JSONResponse(response, http_response.status_code)
    except GitLabClientInvalidToken as error:
        return JSONResponse({"message": str(error)}, 401)
    except GitLabClientRequestError as error:
        return JSONResponse(
            {"message": "Could not retrieve REANA specification from GitLab."},
            error.response.status_code,
        )
    except HTTPError as error:
        return JSONResponse(error.response.json(), error.response.status_code)
    except REANAQuotaExceededError as error:
        return JSONResponse({"message": error.message}, 403)
    except (KeyError, REANAValidationError, ValidationError) as error:
        return JSONResponse({"message": str(error)}, 400)
    except ValueError as error:
        return JSONResponse({"message": str(error)}, 403)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)


@router.post("/workflows/{workflow_id_or_name}/start", summary="Start a workflow")
def start_workflow(
    workflow_id_or_name: str,
    payload: Optional[dict] = Body(None),
    user: User = _RoleUser,
):
    """Submit a workflow to the run queue (quota-checked)."""
    if user.has_exceeded_quota():
        return JSONResponse({"message": get_quota_excess_message(user)}, 403)
    response, status_code = _start_workflow(
        workflow_id_or_name, user, **(payload or {})
    )
    # _start_workflow returns DB-derived values (e.g. UUID workflow_id);
    # jsonable_encoder serializes them the way Flask's jsonify did.
    return JSONResponse(jsonable_encoder(response), status_code)


@router.put(
    "/workflows/{workflow_id_or_name}/status", summary="Set workflow status"
)
def set_workflow_status(
    workflow_id_or_name: str,
    status: str = Query(...),
    payload: Optional[dict] = Body(None),
    user: User = _RoleUser,
):
    """Start/stop/delete/restart a workflow."""
    try:
        if status == "start":
            # Go through the queue (do not call the controller directly).
            response, status_code = _start_workflow(
                workflow_id_or_name, user, **(payload or {})
            )
            if isinstance(response, dict):
                response.pop("run_number", None)
            return JSONResponse(jsonable_encoder(response), status_code)
        response, http_response = current_rwc_api_client.api.set_workflow_status(
            user=str(user.id_),
            workflow_id_or_name=workflow_id_or_name,
            status=status,
            parameters=payload,
        ).result()
        return JSONResponse(
            content=response, status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.get(
    "/workflows/{workflow_id_or_name}/retention_rules",
    summary="Workflow retention rules",
)
def get_workflow_retention_rules(workflow_id_or_name: str, user: User = _RoleUser):
    """Return a workflow's retention rules (controller proxy)."""
    try:
        response, http_response = (
            current_rwc_api_client.api.get_workflow_retention_rules(
                user=str(user.id_), workflow_id_or_name=workflow_id_or_name
            ).result()
        )
        return JSONResponse(
            content=response, status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.get(
    "/workflows/{workflow_id_or_name_a}/diff/{workflow_id_or_name_b}",
    summary="Diff two workflows",
)
def get_workflow_diff(
    workflow_id_or_name_a: str,
    workflow_id_or_name_b: str,
    brief: bool = Query(False),
    # The controller's OpenAPI declares context_lines as a string.
    context_lines: str = Query("5"),
    user: User = _RoleUser,
):
    """Return the diff between two workflows (controller proxy)."""
    try:
        response, http_response = current_rwc_api_client.api.get_workflow_diff(
            user=str(user.id_),
            brief=brief,
            context_lines=context_lines,
            workflow_id_or_name_a=workflow_id_or_name_a,
            workflow_id_or_name_b=workflow_id_or_name_b,
        ).result()
        return JSONResponse(
            content=response, status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.get(
    "/workflows/{workflow_id_or_name}/disk_usage", summary="Workflow disk usage"
)
def get_workflow_disk_usage(
    workflow_id_or_name: str,
    payload: Optional[dict] = Body(None),
    user: User = _RoleUser,
):
    """Return a workflow's workspace disk usage (computed locally)."""
    try:
        parameters = payload or {}
        workflow = _get_workflow_with_uuid_or_name(
            workflow_id_or_name, str(user.id_), True
        )
        disk_usage_info = workflow.get_workspace_disk_usage(
            summarize=bool(parameters.get("summarize", False)),
            search=parameters.get("search"),
        )
        return JSONResponse(
            jsonable_encoder(
                {
                    "workflow_id": str(workflow.id_),
                    "workflow_name": workflow.name,
                    "user": str(user.id_),
                    "disk_usage_info": disk_usage_info,
                }
            )
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


_ONE_TARGET = (
    "Exactly one share target is required: either "
    "'user_email_to_share_with' or 'group_provider' + 'group_id'."
)
_GROUP_PAIR = "Fields 'group_provider' and 'group_id' must be given together."


@router.post("/workflows/{workflow_id_or_name}/share", summary="Share a workflow")
def share_workflow(
    workflow_id_or_name: str,
    payload: Optional[dict] = Body(None),
    user: User = _RoleUser,
):
    """Share a workflow with a user (controller) or a group (local)."""
    payload = payload or {}
    user_email = payload.get("user_email_to_share_with")
    group_provider = payload.get("group_provider")
    group_id = payload.get("group_id")
    has_group_target = group_provider is not None or group_id is not None
    if bool(user_email) == has_group_target:
        return JSONResponse({"message": _ONE_TARGET}, 400)
    if has_group_target and not (group_provider and group_id):
        return JSONResponse({"message": _GROUP_PAIR}, 400)

    if has_group_target:
        try:
            workflow = _get_workflow_with_uuid_or_name(
                workflow_id_or_name, str(user.id_)
            )
            share_workflow_with_group(
                workflow,
                group_provider,
                group_id,
                message=payload.get("message"),
                valid_until=parse_valid_until(payload.get("valid_until")),
            )
            return {
                "message": "The workflow has been shared with the group.",
                "workflow_id": str(workflow.id_),
                "workflow_name": workflow.get_full_workflow_name(),
            }
        except GroupShareValidationError as error:
            return JSONResponse({"message": str(error)}, 400)
        except GroupNotFoundError as error:
            return JSONResponse({"message": str(error)}, 404)
        except GroupShareConflictError as error:
            return JSONResponse({"message": str(error)}, 409)
        except GroupBackendUnavailableError as error:
            return JSONResponse({"message": str(error)}, 503)
        except Exception as error:  # noqa: BLE001
            return _rwc_error(error)

    try:
        share_details = {
            key: payload[key]
            for key in ("message", "valid_until")
            if payload.get(key) is not None
        }
        share_details["user_email_to_share_with"] = user_email
        response, _ = current_rwc_api_client.api.share_workflow(
            workflow_id_or_name=workflow_id_or_name,
            user=str(user.id_),
            share_details=share_details,
        ).result()
        return JSONResponse(content=response, status_code=200)
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.post(
    "/workflows/{workflow_id_or_name}/unshare", summary="Unshare a workflow"
)
def unshare_workflow(
    workflow_id_or_name: str,
    payload: Optional[dict] = Body(None),
    user: User = _RoleUser,
):
    """Unshare a workflow from a user (controller) or a group (local)."""
    payload = payload or {}
    user_email = payload.get("user_email_to_unshare_with")
    group_provider = payload.get("group_provider")
    group_id = payload.get("group_id")
    has_group_target = group_provider is not None or group_id is not None
    if bool(user_email) == has_group_target:
        return JSONResponse(
            {"message": _ONE_TARGET.replace("share_with", "unshare_with")}, 400
        )
    if has_group_target and not (group_provider and group_id):
        return JSONResponse({"message": _GROUP_PAIR}, 400)

    if has_group_target:
        try:
            workflow = _get_workflow_with_uuid_or_name(
                workflow_id_or_name, str(user.id_)
            )
            unshare_workflow_with_group(workflow, group_provider, group_id)
            return {
                "message": "The workflow has been unshared with the group.",
                "workflow_id": str(workflow.id_),
                "workflow_name": workflow.get_full_workflow_name(),
            }
        except GroupNotFoundError as error:
            return JSONResponse({"message": str(error)}, 404)
        except Exception as error:  # noqa: BLE001
            return _rwc_error(error)

    try:
        response, _ = current_rwc_api_client.api.unshare_workflow(
            workflow_id_or_name=workflow_id_or_name,
            user_email_to_unshare_with=user_email,
            user=str(user.id_),
        ).result()
        return JSONResponse(content=response, status_code=200)
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.get(
    "/workflows/{workflow_id_or_name}/share-status",
    summary="Workflow share status",
)
def get_workflow_share_status(workflow_id_or_name: str, user: User = _RoleUser):
    """Return a workflow's user shares (controller) + group shares (local)."""
    try:
        response, _ = current_rwc_api_client.api.get_workflow_share_status(
            workflow_id_or_name=workflow_id_or_name, user=str(user.id_)
        ).result()
        workflow = _get_workflow_with_uuid_or_name(
            workflow_id_or_name, str(user.id_)
        )
        response["shared_with_groups"] = get_group_shares_for_workflow(workflow)
        return JSONResponse(jsonable_encoder(response), 200)
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.post(
    "/workflows/{workflow_id_or_name}/workspace", summary="Upload a workspace file"
)
async def upload_file(
    workflow_id_or_name: str,
    request: Request,
    file_name: Optional[str] = Query(None),
    user: User = _RoleUser,
):
    """Upload a file (application/octet-stream) into the workspace."""
    if user.has_exceeded_quota():
        return JSONResponse({"message": get_quota_excess_message(user)}, 403)
    try:
        if not file_name:
            return JSONResponse({"message": "No file_name provided"}, 400)
        content_type = request.headers.get("content-type") or ""
        if "application/octet-stream" not in content_type:
            return JSONResponse(
                {
                    "message": f"Wrong Content-Type {content_type} use "
                    "application/octet-stream"
                },
                400,
            )
        # NOTE: the body is buffered in memory (the Flask version streamed it);
        # true streaming returns with the reana-commons httpx client (RC-1).
        body = await request.body()
        prevent_disk_quota_excess(
            user, len(body), action=f"Uploading file {file_name}"
        )
        api_url = current_rwc_api_client.swagger_spec.__dict__.get("api_url")
        endpoint = current_rwc_api_client.api.upload_file.operation.path_name.format(
            workflow_id_or_name=workflow_id_or_name
        )
        http_response = await run_in_threadpool(
            requests.post,
            urljoin(api_url, endpoint),
            data=body,
            params={"user": str(user.id_), "file_name": file_name},
            headers={"Content-Type": "application/octet-stream"},
        )
        return JSONResponse(http_response.json(), http_response.status_code)
    except (REANAQuotaExceededError, ValueError) as error:
        return JSONResponse({"message": str(error)}, 403)
    except HTTPError as error:
        return JSONResponse(error.response.json(), error.response.status_code)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)


@router.get(
    "/workflows/{workflow_id_or_name}/workspace/{file_name:path}",
    summary="Download a workspace file",
)
def download_file(
    workflow_id_or_name: str,
    file_name: str,
    preview: bool = Query(False),
    user: User = _RoleUser,
):
    """Stream a workspace file from the controller back to the client."""
    try:
        api_url = current_rwc_api_client.swagger_spec.__dict__.get("api_url")
        endpoint = current_rwc_api_client.api.download_file.operation.path_name.format(
            workflow_id_or_name=workflow_id_or_name, file_name=file_name
        )
        upstream = requests.get(
            urljoin(api_url, endpoint),
            params={"preview": preview, "user": str(user.id_)},
            stream=True,
        )
        headers = {}
        if upstream.headers.get("Content-Disposition"):
            headers["Content-Disposition"] = upstream.headers["Content-Disposition"]
        return StreamingResponse(
            upstream.iter_content(chunk_size=1024),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("Content-Type"),
            headers=headers,
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.delete(
    "/workflows/{workflow_id_or_name}/workspace/{file_name:path}",
    summary="Delete a workspace file",
)
def delete_file(workflow_id_or_name: str, file_name: str, user: User = _RoleUser):
    """Delete a workspace file (controller proxy)."""
    try:
        _, http_response = current_rwc_api_client.api.delete_file(
            user=str(user.id_),
            workflow_id_or_name=workflow_id_or_name,
            file_name=file_name,
        ).result()
        return JSONResponse(http_response.json(), http_response.status_code)
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.put(
    "/workflows/move_files/{workflow_id_or_name}", summary="Move workspace files"
)
def move_files(
    workflow_id_or_name: str,
    source: str = Query(...),
    target: str = Query(...),
    user: User = _RoleUser,
):
    """Move files within a workspace (controller proxy)."""
    try:
        response, http_response = current_rwc_api_client.api.move_files(
            user=str(user.id_),
            workflow_id_or_name=workflow_id_or_name,
            source=source,
            target=target,
        ).result()
        return JSONResponse(
            content=response, status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.post("/workflows/{workflow_id_or_name}/prune", summary="Prune workspace")
def prune_workspace(
    workflow_id_or_name: str,
    payload: Optional[dict] = Body(None),
    user: User = _RoleUser,
):
    """Delete workspace files that are neither inputs nor outputs."""
    payload = payload or {}
    include_inputs = bool(payload.get("include_inputs", False))
    include_outputs = bool(payload.get("include_outputs", False))
    try:
        which_to_keep = InOrOut.INPUTS_OUTPUTS
        if include_inputs:
            which_to_keep = InOrOut.OUTPUTS
        if include_outputs:
            which_to_keep = InOrOut.INPUTS
            if include_inputs:
                which_to_keep = InOrOut.NONE
        workflow = _get_workflow_with_uuid_or_name(
            workflow_id_or_name, str(user.id_)
        )
        deleter = Deleter(workflow)
        for file_or_dir in workspace.iterdir(deleter.workspace, ""):
            deleter.delete_files(which_to_keep, file_or_dir)
        return jsonable_encoder(
            {
                "message": "The workspace has been correctly pruned.",
                "workflow_id": str(workflow.id_),
                "workflow_name": workflow.name,
            }
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.post(
    "/workflows/{workflow_id_or_name}/open/{interactive_session_type}",
    summary="Open an interactive session",
)
def open_interactive_session(
    workflow_id_or_name: str,
    interactive_session_type: str,
    payload: Optional[dict] = Body(None),
    user: User = _RoleUser,
):
    """Open an interactive session (e.g. Jupyter) in the workspace."""
    if user.has_exceeded_quota():
        return JSONResponse({"message": get_quota_excess_message(user)}, 403)
    try:
        if interactive_session_type not in InteractiveSessionType.__members__:
            return JSONResponse(
                {
                    "message": "Interactive session type {0} not found, try "
                    "with one of: {1}".format(
                        interactive_session_type,
                        [e.name for e in InteractiveSessionType],
                    )
                },
                404,
            )
        response, http_response = (
            current_rwc_api_client.api.open_interactive_session(
                user=str(user.id_),
                workflow_id_or_name=workflow_id_or_name,
                interactive_session_type=interactive_session_type,
                interactive_session_configuration=payload,
            ).result()
        )
        return JSONResponse(
            content=response, status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.post(
    "/workflows/{workflow_id_or_name}/close/",
    summary="Close an interactive session",
)
def close_interactive_session(workflow_id_or_name: str, user: User = _RoleUser):
    """Close the workflow's open interactive session (controller proxy)."""
    try:
        response, http_response = (
            current_rwc_api_client.api.close_interactive_session(
                user=str(user.id_), workflow_id_or_name=workflow_id_or_name
            ).result()
        )
        return JSONResponse(
            content=response, status_code=http_response.status_code
        )
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)


@router.get(
    "/workflows/{workflow_id_or_name}/interactive-session-secret",
    summary="Interactive session secret (owner-only)",
)
def get_interactive_session_secret(
    workflow_id_or_name: str, user: User = _RoleUser
):
    """Return the per-session notebook access secret for the owner."""
    try:
        workflow = _get_workflow_with_uuid_or_name(
            workflow_id_or_name, str(user.id_)
        )
        open_session = next(
            (
                session
                for session in workflow.sessions
                if session.session_secret and session.status != RunStatus.deleted
            ),
            None,
        )
        if open_session is None:
            return JSONResponse(
                {"message": "The workflow has no open interactive session."}, 404
            )
        return {
            "session_secret": open_session.session_secret,
            "path": open_session.path,
        }
    except Exception as error:  # noqa: BLE001
        return _rwc_error(error)
