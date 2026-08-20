# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server workflow-functionality Flask-Blueprint."""

import json
import functools
import logging
import os
import shutil
import traceback
import tempfile
import uuid

import requests
from bravado.exception import BravadoTimeoutError, HTTPError
from flask import Blueprint, Response, jsonify, request, stream_with_context
from jsonschema.exceptions import ValidationError
from werkzeug.exceptions import RequestEntityTooLarge
from reana_commons import workspace
from reana_commons.config import (
    REANA_WORKFLOW_ENGINES,
    SHARED_VOLUME_PATH,
    WORKFLOW_RUNTIME_USER_GID,
    WORKFLOW_RUNTIME_USER_UID,
)
from reana_commons.errors import (
    REANAQuotaExceededError,
    REANASpecificationPathError,
    REANAValidationError,
)
from reana_commons.specification_paths import (
    SPECIFICATION_BUNDLE_MAX_PATH_BYTES,
    open_regular_file_beneath,
)
from reana_commons.validation.environments import iter_environment_tag_warnings
from reana_commons.validation.images import iter_image_environments
from reana_commons.validation.report import (
    MAX_VALIDATION_REPORT_ENTRIES,
    REPORT_TRUNCATED_CODE,
)
from reana_commons.validation.operational_options import validate_operational_options
from reana_commons.validation.utils import (
    MAX_LOAD_ERROR_MESSAGE_CHARS,
    bound_error_message,
    validate_workflow_name,
)
from reana_db.database import Session
from reana_db.models import (
    InteractiveSessionType,
    RunStatus,
    ResourceType,
    Workflow,
    WorkflowResource,
    WorkspaceRetentionRule,
    WorkspaceRetentionRuleStatus,
)
from reana_db.utils import (
    _get_workflow_with_uuid_or_name,
    build_workspace_path,
    get_disk_usage_or_zero,
    get_default_quota_resource,
    store_workflow_disk_quota,
    update_users_disk_quota,
)
from reana_server.api_client import current_rwc_api_client
from reana_server.config import (
    REANA_HOSTNAME,
    REANA_SPEC_BUNDLE_MAX_BYTES,
    REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES,
    RWC_MUTATION_CONNECT_TIMEOUT,
    RWC_MUTATION_READ_TIMEOUT,
)
from reana_server.decorators import check_quota, signin_required
from reana_server.deleter import Deleter, InOrOut
from reana_server.gitlab_client import (
    GitLabClient,
    GitLabClientRequestError,
    GitLabClientInvalidToken,
)
from reana_server.fetcher import extract_streamed_zip_response
from reana_server.specification_bundles import (
    extract_uploaded_bundle,
    seed_workspace,
    stage_validation_snapshot,
    workspace_seed_members,
)
from reana_server.utils import (
    RequestStreamWithLen,
    _fail_gitlab_commit_build_status,
    _get_reana_yaml_from_gitlab,
    clone_workflow,
    ensure_dask_service,
    get_fetched_workflows_dir,
    get_quota_excess_message,
    get_workspace_retention_rules,
    is_uuid_v4,
    prevent_disk_quota_excess,
    publish_workflow_submission,
)
from reana_server.validation import (
    REANA_SPEC_FILENAMES,
    SpecValidationServiceError,
    has_reana_spec_file,
    load_and_validate_spec,
    load_raw_spec_mapping,
    validate_input_parameters,
    validate_loaded_spec,
    validate_spec_bundle,
)
from reana_server.workspace_mutations import (
    WorkspaceMutationConflict,
    WorkspaceMutationUnavailable,
    workflow_creation_mutation_lock,
    workflow_family_mutation_lock,
    workspace_mutation_lock,
    workspace_mutation_locks,
)
from reana_server.workflow_creation import create_workflow_on_controller
import marshmallow
from webargs import fields, validate
from webargs.flaskparser import use_kwargs

try:
    from urllib import parse as urlparse
except ImportError:
    from urlparse import urlparse

blueprint = Blueprint("workflows", __name__)

# Maximum bytes a single multipart part may buffer in RAM before Werkzeug spills
# it to a temporary file. Kept small so an untrusted bundle upload cannot pin a
# large amount of server memory during multipart parsing.
_BUNDLE_MAX_FORM_MEMORY_SIZE = 1024 * 1024

_INTERNAL_ERROR_MESSAGE = "An internal server error occurred."
_FILE_COPY_CHUNK_SIZE = 1024 * 1024


_RWC_MUTATION_REQUEST_OPTIONS = {
    "connect_timeout": RWC_MUTATION_CONNECT_TIMEOUT,
    "timeout": RWC_MUTATION_READ_TIMEOUT,
}
# Backward-compatible private alias for focused server tests and extensions.
_workspace_mutation_lock = workspace_mutation_lock


def _legacy_specification_message(replacement_hint):
    """Explain that a released client is talking the retired JSON protocol.

    A released client serializes the workflow specification itself and sends it
    as a JSON ``reana_specification`` member. The server no longer accepts a
    client-serialized specification -- it loads and validates the raw
    specification bundle authoritatively -- so the pairing cannot work and must
    fail with an upgrade instruction rather than an unknown-field complaint.
    """
    return (
        "This server no longer accepts a client-serialized 'reana_specification'. "
        "{} Please upgrade REANA client to a version that uploads the workflow "
        "specification bundle, or connect to an older REANA cluster.".format(
            replacement_hint
        )
    )


def _serialize_workspace_mutation(view):
    """Run a workspace-mutating API view under the shared workspace lock."""

    @functools.wraps(view)
    def wrapped(workflow_id_or_name, *args, **kwargs):
        user = kwargs["user"]
        try:
            try:
                workflow = _get_workflow_with_uuid_or_name(
                    workflow_id_or_name, str(user.id_)
                )
                lock_key = workflow.workspace_path
            except ValueError:
                # Preserve the existing proxy contract: the controller remains
                # authoritative for unknown identifiers. Real server workflows
                # resolve to their shared workspace and therefore also collide
                # across UUID/name aliases and restart run numbers.
                lock_key = "workflow-id-or-name:{}".format(workflow_id_or_name)
            with _workspace_mutation_lock(lock_key):
                return view(workflow_id_or_name, *args, **kwargs)
        except WorkspaceMutationConflict:
            return (
                jsonify(
                    {"message": "The workflow workspace is currently being modified."}
                ),
                409,
            )
        except WorkspaceMutationUnavailable:
            return (
                jsonify(
                    _internal_error_response(
                        "Workspace mutation serialization is unavailable."
                    )
                ),
                503,
            )

    return wrapped


def _internal_error_response(context):
    """Return an opaque 500 response while retaining diagnostic details in logs."""
    logging.exception(context)
    return {"message": _INTERNAL_ERROR_MESSAGE}


def _compensate_failed_workflow_create(workflow, user):
    """Clean up a partially completed post-create workflow operation.

    Each cleanup action converges on the desired final state and is therefore
    safe to retry: the row is marked deleted, the workspace is absent, workflow
    disk usage is recalculated as zero, and user disk usage is recalculated from
    all workflow resources. Failures in one cleanup action do not prevent the
    remaining actions from being attempted.
    """
    # The failed quota operation may have left the scoped session in a failed
    # transaction. Start compensation from a clean transaction boundary.
    Session.rollback()

    try:
        workflow.status = RunStatus.deleted
        Session.commit()
    except Exception:
        Session.rollback()
        logging.exception(
            "Could not mark partially created workflow %s as deleted.", workflow.id_
        )

    shutil.rmtree(workflow.workspace_path, ignore_errors=True)

    try:
        store_workflow_disk_quota(
            workflow, bytes_to_sum=None, override_policy_checks=True
        )
    except Exception:
        Session.rollback()
        logging.exception(
            "Could not reset disk quota for partially created workflow %s.",
            workflow.id_,
        )

    try:
        update_users_disk_quota(user, override_policy_checks=True)
    except Exception:
        Session.rollback()
        logging.exception(
            "Could not recalculate disk quota for user %s after workflow %s "
            "creation failed.",
            user.id_,
            workflow.id_,
        )


def _is_truthy_arg(value):
    """Interpret a query-string flag (``?environments=true``) as a boolean."""
    return str(value).lower() in ("1", "true", "yes", "on")


def _add_bounded_environments(report, reana_specification):
    """Add environments and tag warnings within the global report budget."""
    errors = list(report.get("errors", []))
    warnings = []
    truncated = False
    environments_truncated = False
    overlong_environment_omitted = False
    for warning in report.get("warnings", []):
        if warning.get("code") == REPORT_TRUNCATED_CODE:
            truncated = True
        else:
            warnings.append(warning)
    original_warning_count = len(warnings)
    environments = []

    def append(target, entry):
        nonlocal truncated
        if len(errors) + len(warnings) + len(environments) >= (
            MAX_VALIDATION_REPORT_ENTRIES
        ):
            truncated = True
            return False
        target.append(entry)
        return True

    image_names = []
    for environment in iter_image_environments(
        reana_specification,
        int(WORKFLOW_RUNTIME_USER_UID),
        int(WORKFLOW_RUNTIME_USER_GID),
    ):
        environment = dict(environment)
        if len(environment["image"]) > MAX_LOAD_ERROR_MESSAGE_CHARS:
            truncated = True
            environments_truncated = True
            overlong_environment_omitted = True
            continue
        if not append(environments, environment):
            environments_truncated = True
            break
        image_names.append(environment["image"])

    if overlong_environment_omitted:
        append(
            warnings,
            {
                "code": "environment_identity_omitted",
                "message": (
                    "A runtime environment identity was omitted because its "
                    "image reference is too long."
                ),
                "path": "",
            },
        )

    for finding in iter_environment_tag_warnings(image_names):
        warning = {
            "code": bound_error_message(finding["code"]),
            "message": bound_error_message(finding["message"]),
            "path": bound_error_message(finding["image"]),
        }
        if not append(warnings, warning):
            break

    if truncated:
        while (
            len(errors) + len(warnings) + len(environments)
            >= MAX_VALIDATION_REPORT_ENTRIES
        ):
            if len(warnings) > original_warning_count:
                warnings.pop()
            elif environments:
                environments.pop()
                environments_truncated = True
            elif warnings:
                warnings.pop()
            else:
                # Only pre-existing errors remain. Never drop a real validation
                # error to make room for the marker: each error message is
                # already bounded, so one extra marker entry cannot blow the
                # response-size budget the entry count only approximates.
                break
        warnings.append(
            {
                "code": REPORT_TRUNCATED_CODE,
                "message": "Additional validation findings were omitted.",
                "path": "",
            }
        )
    report["errors"] = errors
    report["warnings"] = warnings
    report["environments"] = environments
    report["environments_truncated"] = environments_truncated


def _validate_spec_bundle_request_size():
    """Reject oversized bundle requests before multipart parsing starts.

    Fast up-front check for the common case where a ``Content-Length`` header is
    present. It does *not* cover chunked uploads (no ``Content-Length``); the
    per-view request-level cap set by :func:`_cap_bundle_request_body` handles
    those.
    """
    if (
        request.content_length is not None
        and request.content_length > REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES
    ):
        raise RequestEntityTooLarge(
            description="Specification bundle request is too large "
            "(maximum is {} bytes).".format(REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES)
        )


def _cap_bundle_request_body(max_form_parts=1):
    """Bound the request body size *before* multipart parsing is triggered.

    Setting ``request.max_content_length`` (reliable on Flask/Werkzeug >= 3.1)
    makes Werkzeug abort parsing with ``RequestEntityTooLarge`` (413) as soon as
    the body exceeds the cap -- including a **chunked** upload that carries no
    ``Content-Length`` header (which slips past
    :func:`_validate_spec_bundle_request_size`). Without this, accessing
    ``request.files`` on a chunked upload spools the entire multipart body to
    server-local temp before any per-member cap can run.

    ``request.max_form_memory_size`` is kept small so no single part is fully
    buffered in RAM before Werkzeug spills it to a temporary file.

    This is a **per-view** cap (deliberately not a global ``MAX_CONTENT_LENGTH``,
    which Werkzeug >= 3.1 would also enforce on ``request.stream`` and thereby
    break the large streaming workspace uploads handled by ``upload_file``). It
    must be called before ``request.files`` is accessed, otherwise the body has
    already been parsed with the default (unbounded) limits.
    """
    request.max_content_length = REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES
    request.max_form_memory_size = _BUNDLE_MAX_FORM_MEMORY_SIZE
    request.max_form_parts = max_form_parts


def _remaining_stream_size(stream):
    """Return remaining bytes in a seekable multipart file stream.

    Werkzeug fully parses file parts into seekable temporary streams. Their
    position must be restored so the downstream controller receives exactly the
    bytes whose length is returned here.
    """
    try:
        position = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell() - position
        stream.seek(position)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise REANAValidationError(
            "Could not determine the uploaded file size."
        ) from exc
    if size < 0:
        raise REANAValidationError("Could not determine the uploaded file size.")
    return size


def _stage_validation_bundle(files):
    """Securely extract the single uploaded ZIP bundle."""
    if set(files) != {"bundle"}:
        raise REANAValidationError(
            "Upload exactly one specification archive in the 'bundle' field."
        )
    bundles = (
        files.getlist("bundle") if hasattr(files, "getlist") else [files["bundle"]]
    )
    if len(bundles) != 1:
        raise REANAValidationError(
            "Upload exactly one specification archive in the 'bundle' field."
        )
    return extract_uploaded_bundle(bundles[0], SHARED_VOLUME_PATH)


@blueprint.route("/workflows/validate", methods=["POST"])
@signin_required()
def validate_workflow_specification(user):  # noqa
    r"""Validate a raw REANA workflow specification bundle.

    ---
    post:
      summary: Validate a raw workflow specification bundle.
      description: >-
        Accepts one uncompressed ZIP validation snapshot in the multipart
        ``bundle`` field. The archive contains canonical ``reana.yaml`` plus its
        explicitly declared workflow/configuration files. Serial specs are loaded
        and validated in-process; Snakemake/CWL/Yadage specs -- whose loading
        executes user code -- are validated inside a sandboxed job spawned by
        reana-workflow-controller. Returns a structured validation report.
      operationId: validate_workflow_specification
      consumes:
        - multipart/form-data
      produces:
        - application/json
      parameters:
        - name: bundle
          in: formData
          description: Uncompressed ZIP validation snapshot containing canonical
            reana.yaml plus explicitly declared workflow sources.
          required: true
          type: file
        - name: access_token
          in: query
          required: false
          type: string
        - name: environments
          in: query
          description: If true, run offline reproducibility checks on runtime
            environment image references and return the loaded image and
            effective runtime UID/GID combinations so the client can optionally
            pull and inspect them locally.
          required: false
          type: boolean
      responses:
        200:
          description: Validation ran; a structured report is returned.
          schema:
            type: object
            properties:
              valid:
                type: boolean
              errors:
                type: array
                items:
                  type: object
                  properties:
                    code:
                      type: string
                    message:
                      type: string
                    path:
                      type: string
              warnings:
                type: array
                items:
                  type: object
              environments:
                description: Distinct runtime image and effective UID/GID
                  combinations of the loaded spec (only when environments is
                  requested), for client-side checks.
                type: array
                items:
                  type: object
                  required:
                    - image
                    - runtime_uid
                    - runtime_gid
                  properties:
                    image:
                      type: string
                    runtime_uid:
                      type: integer
                    runtime_gid:
                      type: integer
              environments_truncated:
                description: Whether one or more requested environment
                  identities were omitted from the bounded response.
                type: boolean
        400:
          description: The bundle was missing or malformed.
          schema:
            $ref: '#/definitions/ErrorResponse'
        401:
          description: Request malformed or missing access token.
          schema:
            $ref: '#/definitions/ErrorResponse'
        403:
          description: Request access forbidden.
          schema:
            $ref: '#/definitions/ErrorResponse'
        413:
          description: The uploaded request exceeds the bounded bundle limit.
          schema:
            type: object
            required:
              - message
            properties:
              message:
                type: string
        429:
          description: Request rate limit exceeded.
          schema:
            type: object
            required:
              - message
            properties:
              message:
                type: string
        500:
          description: Internal error while validating the specification.
          schema:
            type: object
            properties:
              message:
                type: string
    """
    abs_dir = None
    try:
        # Cap the request body *before* touching request.files, so a chunked
        # upload (no Content-Length) cannot make Werkzeug spool the whole
        # multipart body to server-local temp before any size check runs.
        _cap_bundle_request_body()
        _validate_spec_bundle_request_size()
        if not request.files:
            return (
                jsonify({"message": "No specification bundle files were provided."}),
                400,
            )
        bundle = _stage_validation_bundle(request.files)
        abs_dir = bundle.absolute_path
        rel_path = bundle.relative_path
        reana_yaml_path = next(
            (
                os.path.join(abs_dir, name)
                for name in REANA_SPEC_FILENAMES
                if os.path.isfile(os.path.join(abs_dir, name))
            ),
            None,
        )
        if not reana_yaml_path:
            return (
                jsonify({"message": "No reana.yaml found in the uploaded bundle."}),
                400,
            )
        report = validate_spec_bundle(abs_dir, rel_path)
        # Optional offline runtime-environment checks. The server never contacts
        # registries; clients may explicitly pull and inspect returned identities.
        if _is_truthy_arg(request.args.get("environments")) and report.get(
            "reana_specification"
        ):
            _add_bounded_environments(
                report,
                report["reana_specification"],
            )
        # The expanded specification is an internal validation artifact. It can
        # be as large as the uploaded snapshot and neither supported client uses
        # it, so do not reflect it in the bounded control-plane response.
        report.pop("reana_specification", None)
        return jsonify(report), 200
    except REANAValidationError as e:
        # A genuinely invalid *specification* is a client error (400); note the
        # unloadable-spec case is already reported as 200 ``valid:false`` above.
        return jsonify({"message": str(e)}), 400
    except SpecValidationServiceError:
        # The validation *service* itself failed (controller unreachable / a
        # non-OK controller response / the sandbox exited with the internal-error
        # code). That is a server-side outage, not an invalid specification, so it
        # must surface as 500 -- never as a 200 ``valid:false`` report that a
        # client would render as "X is not a valid REANA specification".
        return (
            jsonify(
                _internal_error_response(
                    "Workflow specification validation service failure."
                )
            ),
            500,
        )
    except RequestEntityTooLarge as e:
        return jsonify({"message": e.description or str(e)}), 413
    except Exception:
        return (
            jsonify(
                _internal_error_response(
                    "Unexpected error while validating a workflow specification."
                )
            ),
            500,
        )
    finally:
        if abs_dir:
            shutil.rmtree(abs_dir, ignore_errors=True)


@blueprint.route("/workflows", methods=["GET"])
@use_kwargs(
    {
        "page": fields.Int(validate=validate.Range(min=1)),
        "size": fields.Int(validate=validate.Range(min=1)),
        "include_progress": fields.Bool(),
        "include_workspace_size": fields.Bool(),
        "workflow_id_or_name": fields.Str(),
        "shared": fields.Bool(),
        "shared_by": fields.Str(),
        "shared_with": fields.Str(),
    },
    location="query",
    unknown=marshmallow.EXCLUDE,
)
@signin_required(token_required=False)
def get_workflows(user, **kwargs):  # noqa
    r"""Get all current workflows in REANA.

    ---
    get:
      summary: Returns list of all current workflows in REANA.
      description: >-
        This resource return all current workflows in JSON format.
      operationId: get_workflows
      produces:
       - application/json
      parameters:
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: type
          in: query
          description: Required. Type of workflows.
          required: true
          type: string
        - name: verbose
          in: query
          description: Optional flag to show more information.
          required: false
          type: boolean
        - name: search
          in: query
          description: Filter workflows by name.
          required: false
          type: string
        - name: sort
          in: query
          description: Sort workflows by creation date (asc, desc).
          required: false
          type: string
        - name: status
          in: query
          description: Filter workflows by list of statuses.
          required: false
          type: array
          items:
            type: string
        - name: page
          in: query
          description: Results page number (pagination).
          required: false
          type: integer
        - name: size
          in: query
          description: Number of results per page (pagination).
          required: false
          type: integer
        - name: include_progress
          in: query
          description: Include progress information of the workflows.
          type: boolean
        - name: include_workspace_size
          in: query
          description: Include size information of the workspace.
          type: boolean
        - name: workflow_id_or_name
          in: query
          description: Optional analysis UUID or name to filter.
          required: false
          type: string
        - name: shared
          in: query
          description: Optional flag to list all shared (owned and unowned) workflows.
          required: false
          type: boolean
        - name: shared_by
          in: query
          description: Optional argument to list workflows shared by the specified user.
          required: false
          type: string
        - name: shared_with
          in: query
          description: Optional argument to list workflows shared with the specified user.
          required: false
          type: string
      responses:
        200:
          description: >-
            Request succeeded. The response contains the list of all workflows.
          schema:
            type: object
            properties:
              total:
                type: integer
              items:
                type: array
                items:
                  type: object
                  properties:
                    id:
                      type: string
                    name:
                      type: string
                    status:
                      type: string
                    size:
                      type: object
                      properties:
                        raw:
                          type: integer
                        human_readable:
                          type: string
                    user:
                      type: string
                    launcher_url:
                      type: string
                      x-nullable: true
                    owner_email:
                        type: string
                    shared_with:
                        type: array
                        items:
                          type: string
                    created:
                      type: string
                    session_status:
                      type: string
                    session_type:
                      type: string
                    session_uri:
                      type: string
                    progress:
                      type: object
                      properties:
                        current_command:
                          type: string
                          x-nullable: true
                        current_step_name:
                          type: string
                          x-nullable: true
                        failed:
                          properties:
                            job_ids:
                              items:
                                type: string
                              type: array
                            total:
                              type: integer
                          type: object
                        finished:
                          properties:
                            job_ids:
                              items:
                                type: string
                              type: array
                            total:
                              type: integer
                          type: object
                        run_finished_at:
                          type: string
                          x-nullable: true
                        run_started_at:
                          type: string
                          x-nullable: true
                        run_stopped_at:
                          type: string
                          x-nullable: true
                        running:
                          properties:
                            job_ids:
                              items:
                                type: string
                              type: array
                            total:
                              type: integer
                          type: object
                        total:
                          properties:
                            job_ids:
                              items:
                                type: string
                              type: array
                            total:
                              type: integer
                          type: object
          examples:
            application/json:
              [
                {
                  "id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                  "name": "mytest.1",
                  "status": "running",
                  "size":{
                    "raw": 10490000,
                    "human_readable": "10 MB"
                  },
                  "user": "00000000-0000-0000-0000-000000000000",
                  "created": "2018-06-13T09:47:35.66097",
                },
                {
                  "id": "3c9b117c-d40a-49e3-a6de-5f89fcada5a3",
                  "name": "mytest.2",
                  "status": "finished",
                  "size":{
                    "raw": 12580000,
                    "human_readable": "12 MB"
                  },
                  "user": "00000000-0000-0000-0000-000000000000",
                  "created": "2018-06-13T09:47:35.66097",
                },
                {
                  "id": "72e3ee4f-9cd3-4dc7-906c-24511d9f5ee3",
                  "name": "mytest.3",
                  "status": "created",
                  "size":{
                    "raw": 184320,
                    "human_readable": "180 KB"
                  },
                  "user": "00000000-0000-0000-0000-000000000000",
                  "created": "2018-06-13T09:47:35.66097",
                },
                {
                  "id": "c4c0a1a6-beef-46c7-be04-bf4b3beca5a1",
                  "name": "mytest.4",
                  "status": "created",
                  "size": {
                    "raw": 1074000000,
                    "human_readable": "1 GB"
                  },
                  "user": "00000000-0000-0000-0000-000000000000",
                  "created": "2018-06-13T09:47:35.66097",
                }
              ]
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Your request contains not valid JSON."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000 does not
                            exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Something went wrong."
              }
    """
    try:
        type_ = request.args.get("type", "batch")
        search = request.args.get("search")
        sort = request.args.get("sort", "desc")
        status = request.args.getlist("status")
        verbose = json.loads(request.args.get("verbose", "false").lower())
        response, http_response = current_rwc_api_client.api.get_workflows(
            user=str(user.id_),
            type=type_,
            search=search,
            sort=sort,
            status=status or None,
            verbose=bool(verbose),
            **kwargs,
        ).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except json.JSONDecodeError:
        logging.error(traceback.format_exc())
        return jsonify({"message": "Your request contains not valid JSON."}), 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows", methods=["POST"])
@signin_required(include_gitlab_login=True)
def create_workflow(user):  # noqa
    r"""Create a workflow.

    ---
    post:
      summary: Creates a new workflow based on a REANA specification file.
      description: >-
        Creates a workflow from one uncompressed ZIP validation snapshot in the
        multipart ``bundle`` field. The archive contains canonical ``reana.yaml``
        plus explicitly declared workflow/parameter files. The server loads and validates the specification
        authoritatively (sandboxed for Snakemake/CWL/Yadage).
      operationId: create_workflow
      consumes:
        - multipart/form-data
      produces:
        - application/json
      parameters:
        - name: workflow_name
          in: query
          description: Name of the workflow to be created. If not provided
            name will be generated.
          required: true
          type: string
        - name: bundle
          in: formData
          description: Uncompressed ZIP validation snapshot containing canonical
            reana.yaml plus explicitly declared workflow sources.
          required: true
          type: file
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
      responses:
        201:
          description: >-
            Request succeeded. The workflow has been created.
          schema:
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
              validation_warnings:
                type: array
                items:
                  type: object
                  properties:
                    code:
                      type: string
                    message:
                      type: string
                    path:
                      type: string
          examples:
            application/json:
              {
                "message": "The workflow has been successfully created.",
                "workflow_id": "cdcf48b1-c2f3-4693-8230-b066e088c6ac",
                "workflow_name": "mytest.1"
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow name cannot be a valid UUIDv4."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000 does not
                            exist."
              }
        413:
          description: The uploaded request exceeds the bounded bundle limit.
          schema:
            type: object
            required:
              - message
            properties:
              message:
                type: string
        409:
          description: Another workflow-family mutation is in progress.
          schema:
            $ref: '#/definitions/ErrorResponse'
        429:
          description: Request rate limit exceeded.
          schema:
            type: object
            required:
              - message
            properties:
              message:
                type: string
        503:
          description: Workspace mutation or controller service is unavailable.
          schema:
            $ref: '#/definitions/ErrorResponse'
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "An internal server error occurred."
              }
        501:
          description: >-
            Request failed. Not implemented.
    """
    bundle_dir = None
    bundle_members = None
    fetched_dir = None
    seed_members = None
    seed_bytes = 0
    try:
        if request.args.get("spec"):
            return jsonify("Not implemented"), 501

        # Parse the JSON body at most once: a released client posts its whole
        # serialized specification here, and this runs before the per-view body
        # cap that only bounds the multipart bundle path.
        json_payload = (request.json or {}) if request.is_json else {}
        request_from_gitlab = "object_kind" in json_payload
        # A released client sends the serialized specification as the *entire*
        # JSON body: the OpenAPI 2.0 body parameter is named
        # ``reana_specification``, but a Swagger body parameter's schema is the
        # body itself, so that name never appears on the wire (the released
        # server read it back as a bare ``request.json``). The only JSON this
        # endpoint still accepts is a GitLab webhook, so any other JSON body is
        # a client that predates the specification-bundle protocol.
        if not request_from_gitlab and json_payload:
            return (
                jsonify(
                    {
                        "message": _legacy_specification_message(
                            "Workflow creation now uploads an uncompressed ZIP "
                            "specification bundle in the multipart 'bundle' field."
                        )
                    }
                ),
                400,
            )
        validation_warnings = []
        if request_from_gitlab:
            (
                reana_spec_file,
                git_url,
                workflow_name,
                git_branch,
                git_commit_sha,
            ) = _get_reana_yaml_from_gitlab(request.json, user.id_)
            fetched_dir = get_fetched_workflows_dir(str(user.id_))
            gitlab_client = GitLabClient.from_k8s_secret(user.id_)
            archive_response = gitlab_client.get_repository_archive(
                git_url, git_commit_sha
            )
            fetcher = extract_streamed_zip_response(
                archive_response,
                fetched_dir,
                spec="reana.yaml",
                workflow_name=workflow_name,
            )
            specification_path = fetcher.workflow_spec_path()
            (
                bundle_dir,
                _bundle_rel,
                _bundle_bytes,
                _legacy_parameters,
            ) = stage_validation_snapshot(specification_path, SHARED_VOLUME_PATH)
            reana_spec_file, validation_warnings = load_and_validate_spec(bundle_dir)
            seed_members, seed_bytes = workspace_seed_members(specification_path)
            prevent_disk_quota_excess(
                user,
                seed_bytes,
                action=f"Creating the workflow {workflow_name}",
            )
            git_metadata = {
                "git_url": git_url,
                "git_branch": git_branch,
                "git_commit_sha": git_commit_sha,
            }
        else:
            # Raw-bundle create: the client uploads the specification bundle
            # (reana.yaml + referenced workflow/parameter files) as multipart
            # form data. The server loads and validates it authoritatively
            # (in-process for serial, sandboxed for Snakemake/CWL/Yadage), so it
            # never trusts a client-serialized specification.
            git_metadata = {}
            workflow_name = request.args.get("workflow_name", "")
            # Reject an over-quota user *before* any expensive work (staging the
            # bundle on the shared volume and spawning a sandbox validation Job).
            if user.has_exceeded_quota():
                raise REANAQuotaExceededError(get_quota_excess_message(user))
            # Cap the request body *before* touching request.files, so a chunked
            # upload (no Content-Length) cannot make Werkzeug spool the whole
            # multipart body to server-local temp before any size check runs.
            _cap_bundle_request_body()
            _validate_spec_bundle_request_size()
            if not request.files:
                raise REANAValidationError(
                    "A workflow specification bundle must be uploaded."
                )
            # Stage the bundle and validate it (B3: create-time lint). Keep it
            # afterwards so it can seed the workspace once the workflow exists
            # (C1); it is removed in the outer ``finally``.
            bundle = _stage_validation_bundle(request.files)
            bundle_dir = bundle.absolute_path
            bundle_bytes = bundle.total_bytes
            bundle_members = bundle.members
            reana_spec_file, validation_warnings = load_and_validate_spec(bundle_dir)
            prevent_disk_quota_excess(
                user, bundle_bytes, action=f"Creating the workflow {workflow_name}"
            )

        if user.has_exceeded_quota() and request_from_gitlab:
            message = f"User quota exceeded. Please check {REANA_HOSTNAME}"
            _fail_gitlab_commit_build_status(user, git_url, git_commit_sha, message)
            return jsonify({"message": "Gitlab webhook was processed"}), 200

        validate_workflow_name(workflow_name)
        if is_uuid_v4(workflow_name):
            return jsonify({"message": "Workflow name cannot be a valid UUIDv4."}), 400

        workflow_engine = reana_spec_file["workflow"]["type"]
        if workflow_engine not in REANA_WORKFLOW_ENGINES:
            raise Exception("Unknown workflow type.")

        operational_options = validate_operational_options(
            workflow_engine, reana_spec_file.get("inputs", {}).get("options", {})
        )

        workspace_root_path = reana_spec_file.get("workspace", {}).get("root_path")
        # Both raw and GitLab snapshots were fully validated before workflow
        # creation; only the declared seed remains to be copied below.

        retention_days = reana_spec_file.get("workspace", {}).get("retention_days")
        retention_rules = get_workspace_retention_rules(retention_days)

        canonical_workflow_name = workflow_name or "workflow"
        workflow_uuid = str(uuid.uuid4())
        workspace_path = build_workspace_path(
            str(user.id_), workflow_uuid, workspace_root_path
        )
        workflow_dict = {
            "reana_specification": reana_spec_file,
            "workflow_name": canonical_workflow_name,
            "workflow_id": workflow_uuid,
            "operational_options": operational_options,
            "retention_rules": retention_rules,
        }
        if git_metadata:
            workflow_dict["git_metadata"] = git_metadata

        with workflow_creation_mutation_lock(
            user.id_, canonical_workflow_name, workspace_path
        ):
            response, http_response = create_workflow_on_controller(
                lambda: current_rwc_api_client.api.create_workflow(
                    workflow=workflow_dict,
                    user=str(user.id_),
                    workspace_root_path=workspace_root_path,
                    _request_options=_RWC_MUTATION_REQUEST_OPTIONS,
                ).result(),
                workflow_uuid,
                user.id_,
                workspace_path,
                lambda created_workflow: _compensate_failed_workflow_create(
                    created_workflow, user
                ),
            )
            returned_workflow_uuid = response.get("workflow_id")
            if returned_workflow_uuid != workflow_uuid:
                if returned_workflow_uuid:
                    try:
                        unexpected_workflow = _get_workflow_with_uuid_or_name(
                            returned_workflow_uuid, str(user.id_)
                        )
                    except ValueError:
                        pass
                    else:
                        _compensate_failed_workflow_create(unexpected_workflow, user)
                raise RuntimeError("Controller returned an unexpected workflow id.")
            workflow = _get_workflow_with_uuid_or_name(workflow_uuid, str(user.id_))
            if os.path.abspath(workflow.workspace_path) != os.path.abspath(
                workspace_path
            ):
                _compensate_failed_workflow_create(workflow, user)
                raise RuntimeError("Controller created an unexpected workspace path.")

            if validation_warnings:
                response["validation_warnings"] = validation_warnings

            if git_metadata:
                try:
                    copied_bytes = seed_workspace(seed_members, workflow.workspace_path)
                    if copied_bytes != seed_bytes:
                        raise REANAValidationError(
                            "The GitLab source changed while its workspace was seeded."
                        )
                except Exception:
                    _compensate_failed_workflow_create(workflow, user)
                    raise
                Session.commit()
                store_workflow_disk_quota(workflow, bytes_to_sum=seed_bytes)
                update_users_disk_quota(user, bytes_to_sum=seed_bytes)

                parameters = request.json
                publish_workflow_submission(workflow, user.id_, parameters)
            elif bundle_dir:
                # Seed the freshly created workspace from the exact validated
                # snapshot while creation still owns its mutation boundary.
                try:
                    copied_bytes = seed_workspace(
                        bundle_members, workflow.workspace_path
                    )
                    if copied_bytes != bundle_bytes:
                        raise REANAValidationError(
                            "The uploaded workflow bundle changed while its "
                            "workspace was seeded."
                        )
                    store_workflow_disk_quota(workflow, bytes_to_sum=bundle_bytes)
                    update_users_disk_quota(user, bytes_to_sum=bundle_bytes)
                except Exception:
                    _compensate_failed_workflow_create(workflow, user)
                    raise
        return jsonify(response), http_response.status_code
    except GitLabClientInvalidToken as e:
        return jsonify({"message": str(e)}), 401
    except GitLabClientRequestError as e:
        logging.error(str(e))
        return (
            jsonify({"message": "Could not retrieve REANA specification from GitLab."}),
            e.response.status_code,
        )
    except HTTPError as e:
        if e.response.status_code >= 500:
            return (
                jsonify(
                    _internal_error_response(
                        "Controller error while creating a workflow."
                    )
                ),
                e.response.status_code,
            )
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except BravadoTimeoutError:
        return jsonify({"message": "Workflow controller request timed out."}), 503
    except WorkspaceMutationConflict:
        return jsonify({"message": "The workflow family is currently changing."}), 409
    except WorkspaceMutationUnavailable:
        return (
            jsonify(
                _internal_error_response(
                    "Workspace mutation serialization is unavailable."
                )
            ),
            503,
        )
    except REANAQuotaExceededError as e:
        if "git_url" in locals() and "git_commit_sha" in locals():
            _fail_gitlab_commit_build_status(user, git_url, git_commit_sha, str(e))
            return jsonify({"message": "Gitlab webhook was processed"}), 200
        return jsonify({"message": e.message}), 403
    except (KeyError, REANAValidationError) as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except RequestEntityTooLarge as e:
        return jsonify({"message": e.description or str(e)}), 413
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception:
        return (
            jsonify(_internal_error_response("Unexpected error creating a workflow.")),
            500,
        )
    finally:
        if bundle_dir:
            shutil.rmtree(bundle_dir, ignore_errors=True)
        if fetched_dir:
            shutil.rmtree(fetched_dir, ignore_errors=True)


@blueprint.route("/workflows/<workflow_id_or_name>/specification", methods=["GET"])
@signin_required()
def get_workflow_specification(workflow_id_or_name, user):  # noqa
    r"""Get workflow specification.

    ---
    get:
      summary: Get the specification used for this workflow run.
      description: >-
        This resource returns the REANA workflow specification used to start
        the workflow run. Resource is expecting a workflow UUID.
      operationId: get_workflow_specification
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: API access_token of workflow owner.
          required: false
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Workflow specification is returned.
          schema:
            type: object
            properties:
              parameters:
                type: object
              specification:
                type: object
                properties:
                  inputs:
                    type: object
                    properties:
                      files:
                        type: array
                        items:
                          type: string
                      directories:
                        type: array
                        items:
                          type: string
                      parameters:
                        type: object
                      options:
                        type: object
                  outputs:
                    type: object
                    properties:
                      files:
                        type: array
                        items:
                          type: string
                      directories:
                        type: array
                        items:
                          type: string
                  version:
                    type: string
                  workflow:
                    type: object
                    properties:
                      specification:
                        type: object
                        x-nullable: true
                        properties:
                          steps:
                            type: array
                            items:
                              type: object
                      type:
                        type: string
                      file:
                        type: string
          examples:
            application/json:
              {
                "parameters": {},
                "specification": {
                  "inputs": {
                    "files": [
                      "code/helloworld.py",
                      "data/names.txt"
                    ],
                    "parameters": {
                      "helloworld": "code/helloworld.py",
                      "inputfile": "data/names.txt",
                      "outputfile": "results/greetings.txt",
                      "sleeptime": 0
                    }
                  },
                  "outputs": {
                    "files": [
                      "results/greetings.txt"
                    ]
                  },
                  "version": "0.3.0",
                  "workflow": {
                    "specification": {
                      "steps": [
                        {
                          "commands": [
                            "python \"${helloworld}\" --inputfile \"${inputfile}\" --outputfile \"${outputfile}\" --sleeptime ${sleeptime}"
                          ],
                          "environment": "python:2.7-slim"
                        }
                      ]
                    },
                    "type": "serial"
                  }
                }
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist"
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")
        workflow = _get_workflow_with_uuid_or_name(
            workflow_id_or_name, str(user.id_), True
        )

        return (
            jsonify(
                {
                    "specification": workflow.reana_specification,
                    # `input_parameters` can be null, if so return an empty dict
                    "parameters": workflow.input_parameters or {},
                }
            ),
            200,
        )
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/logs", methods=["GET"])
@use_kwargs(
    {
        "page": fields.Int(validate=validate.Range(min=1)),
        "size": fields.Int(validate=validate.Range(min=1)),
    },
    location="query",
    unknown=marshmallow.EXCLUDE,
)
@signin_required()
def get_workflow_logs(workflow_id_or_name, user, **kwargs):  # noqa
    r"""Get workflow logs.

    ---
    get:
      summary: Get workflow logs of a workflow.
      description: >-
        This resource reports the status of a workflow.
        Resource is expecting a workflow UUID.
      operationId: get_workflow_logs
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: API access_token of workflow owner.
          required: false
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: steps
          in: body
          description: Steps of a workflow.
          required: false
          schema:
            type: array
            description: List of step names to get logs for.
            items:
              type: string
              description: step name.
        - name: page
          in: query
          description: Results page number (pagination).
          required: false
          type: integer
        - name: size
          in: query
          description: Number of results per page (pagination).
          required: false
          type: integer
      responses:
        200:
          description: >-
            Request succeeded. Info about a workflow, including the status is
            returned.
          schema:
            type: object
            properties:
              workflow_id:
                type: string
              workflow_name:
                type: string
              logs:
                type: string
              user:
                type: string
              live_logs_enabled:
                type: boolean
          examples:
            application/json:
              {
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
                "logs": "<Workflow engine log output>",
                "user": "00000000-0000-0000-0000-000000000000",
                "live_logs_enabled": true
              }
        400:
          description: >-
            Request failed. The incoming data specification seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist"
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        steps = request.json if request.is_json else None
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.get_workflow_logs(
            user=str(user.id_),
            steps=steps or None,
            workflow_id_or_name=workflow_id_or_name,
            **kwargs,
        ).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/status", methods=["GET"])
@signin_required()
def get_workflow_status(workflow_id_or_name, user):  # noqa
    r"""Get workflow status.

    ---
    get:
      summary: Get status of a workflow.
      description: >-
        This resource reports the status of a workflow.
        Resource is expecting a workflow UUID.
      operationId: get_workflow_status
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Info about a workflow, including the status is
            returned.
          schema:
            type: object
            properties:
              id:
                type: string
              name:
                type: string
              created:
                type: string
              status:
                type: string
              user:
                type: string
              progress:
                type: object
                properties:
                  run_started_at:
                    type: string
                    x-nullable: true
                  run_finished_at:
                    type: string
                    x-nullable: true
                  run_stopped_at:
                    type: string
                    x-nullable: true
                  total:
                    type: object
                    properties:
                      total:
                        type: integer
                      job_ids:
                        type: array
                        items:
                          type: string
                  running:
                    type: object
                    properties:
                      total:
                        type: integer
                      job_ids:
                        type: array
                        items:
                          type: string
                  finished:
                    type: object
                    properties:
                      total:
                        type: integer
                      job_ids:
                        type: array
                        items:
                          type: string
                  failed:
                    type: object
                    properties:
                      total:
                        type: integer
                      job_ids:
                        type: array
                        items:
                          type: string
                  current_command:
                    type: string
                    x-nullable: true
                  current_step_name:
                    type: string
                    x-nullable: true
              logs:
                type: string
          examples:
            application/json:
              {
                "created": "2018-10-29T12:50:12",
                "id": "4e576cf9-a946-4346-9cde-7712f8dcbb3f",
                "logs": "",
                "name": "mytest.1",
                "progress": {
                  "current_command": None,
                  "current_step_name": None,
                  "failed": {"job_ids": [], "total": 0},
                  "finished": {"job_ids": [], "total": 0},
                  "run_started_at": "2018-10-29T12:51:04",
                  "running": {"job_ids": [], "total": 0},
                  "total": {"job_ids": [], "total": 1}
                },
                "status": "running",
                "user": "00000000-0000-0000-0000-000000000000"
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either User or Analysis does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Analysis 256b25f4-4cfb-4684-b7a8-73872ef455a1 does
                            not exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.get_workflow_status(
            user=str(user.id_), workflow_id_or_name=workflow_id_or_name
        ).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


def _workspace_has_source_file(workspace_path, relative_path):
    """Return whether ``relative_path`` is a file contained in ``workspace_path``.

    Absolute paths and ``..`` escapes are treated as *absent*: a restart may
    only reference workflow-source files genuinely contained in the workspace.
    """
    if not relative_path:
        return False
    workspace_path = os.path.abspath(workspace_path)
    full_path = os.path.abspath(os.path.join(workspace_path, relative_path))
    if os.path.commonpath([workspace_path, full_path]) != workspace_path:
        return False
    return os.path.isfile(full_path)


def _workspace_specification_path(workspace_path):
    """Return the canonical raw specification path in a workflow workspace."""
    for filename in REANA_SPEC_FILENAMES:
        candidate = os.path.join(workspace_path, filename)
        if os.path.lexists(candidate):
            return candidate
    raise REANAValidationError(
        "No REANA specification file is present in the workflow workspace."
    )


def _stage_workspace_validation_snapshot(workspace_path, specification_path=None):
    """Create one bounded immutable definition snapshot of a workspace."""
    specification_path = specification_path or _workspace_specification_path(
        workspace_path
    )
    staged_directory, _relative_path, _total_bytes, _legacy_parameters = (
        stage_validation_snapshot(
            specification_path,
            SHARED_VOLUME_PATH,
            source_base_directory=workspace_path,
        )
    )
    return staged_directory


def _copy_regular_file(source_root, source_path, destination):
    """Copy one securely opened regular file to a new private destination."""
    relative_source = os.path.relpath(
        os.path.abspath(source_path), os.path.abspath(source_root)
    ).replace(os.sep, "/")
    source_descriptor = open_regular_file_beneath(
        source_root, relative_source, "restart specification"
    )
    source_size = os.fstat(source_descriptor).st_size
    if source_size > REANA_SPEC_BUNDLE_MAX_BYTES:
        os.close(source_descriptor)
        raise REANAValidationError(
            "Restart specification is too large (maximum is {} bytes).".format(
                REANA_SPEC_BUNDLE_MAX_BYTES
            )
        )
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        destination_flags |= os.O_NOFOLLOW
    try:
        destination_descriptor = os.open(destination, destination_flags, 0o600)
    except Exception:
        os.close(source_descriptor)
        raise
    try:
        with os.fdopen(source_descriptor, "rb") as source, os.fdopen(
            destination_descriptor, "wb"
        ) as target:
            copied = 0
            while True:
                chunk = source.read(_FILE_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > REANA_SPEC_BUNDLE_MAX_BYTES:
                    raise REANAValidationError(
                        "Restart specification is too large (maximum is {} bytes).".format(
                            REANA_SPEC_BUNDLE_MAX_BYTES
                        )
                    )
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise


def _promote_restart_specification(workspace_path, staged_directory):
    """Atomically install a validated replacement and retain a rollback copy."""
    try:
        canonical_path = _workspace_specification_path(workspace_path)
        had_original = True
    except REANAValidationError:
        canonical_path = os.path.join(workspace_path, "reana.yaml")
        had_original = False
    suffix = uuid.uuid4().hex
    candidate_path = os.path.join(workspace_path, ".reana-promote-{}".format(suffix))
    backup_path = os.path.join(workspace_path, ".reana-backup-{}".format(suffix))
    if had_original:
        _copy_regular_file(workspace_path, canonical_path, backup_path)
    try:
        _copy_regular_file(
            staged_directory,
            os.path.join(staged_directory, "reana.yaml"),
            candidate_path,
        )
        os.replace(candidate_path, canonical_path)
    except Exception:
        for temporary_path in (candidate_path, backup_path):
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        raise
    return canonical_path, backup_path if had_original else None


def _rollback_restart_specification(promotion):
    """Restore the canonical specification after a failed submission."""
    canonical_path, backup_path = promotion
    if backup_path:
        os.replace(backup_path, canonical_path)
    else:
        try:
            os.unlink(canonical_path)
        except FileNotFoundError:
            pass


def _complete_restart_specification_promotion(promotion):
    """Discard a restart rollback copy after successful submission."""
    _canonical_path, backup_path = promotion
    if backup_path:
        try:
            os.unlink(backup_path)
        except OSError:
            logging.warning("Could not remove restart specification backup.")


def _load_and_validate_workspace_snapshot(workspace_path):
    """Validate only the declared definition snapshot of a mutable workspace."""
    staged_directory = None
    try:
        staged_directory = _stage_workspace_validation_snapshot(workspace_path)
        return load_and_validate_spec(staged_directory)
    finally:
        if staged_directory:
            shutil.rmtree(staged_directory, ignore_errors=True)


def _workspace_retention_rule_states(workspace_path):
    """Return current retention-rule states for every run sharing a workspace."""
    rules = (
        Session.query(WorkspaceRetentionRule)
        .join(Workflow, WorkspaceRetentionRule.workflow_id == Workflow.id_)
        .filter(Workflow.workspace_path == workspace_path)
        .all()
    )
    return {rule.id_: rule.status for rule in rules}


def _recalculate_shared_workspace_quota(workflow, user):
    """Synchronise quota rows for all runs that share one mutable workspace."""
    workspace_size = get_disk_usage_or_zero(
        workflow.workspace_path, override_policy_checks=True
    )
    disk_resource = get_default_quota_resource(ResourceType.disk.name)
    siblings = (
        Session.query(Workflow).filter_by(workspace_path=workflow.workspace_path).all()
    )
    for sibling in siblings:
        resource = (
            Session.query(WorkflowResource)
            .filter_by(workflow_id=sibling.id_, resource_id=disk_resource.id_)
            .one_or_none()
        )
        if resource is None:
            resource = WorkflowResource(
                workflow_id=sibling.id_, resource_id=disk_resource.id_
            )
            Session.add(resource)
        resource.quota_used = workspace_size
    Session.flush()
    update_users_disk_quota(user, override_policy_checks=True)


def _compensate_failed_restart(workflow, retention_rule_states, user):
    """Make a cloned but unsubmitted restart non-live and restore shared state."""
    Session.rollback()
    try:
        # Bypass the mapped status listener: assigning ``workflow.status`` can
        # commit independently, breaking compensation into partially durable
        # steps. Keep clone deletion and retention restoration in one unit.
        Session.query(Workflow).filter(Workflow.id_ == workflow.id_).update(
            {Workflow.status: RunStatus.deleted}, synchronize_session=False
        )
        for rule in Session.query(WorkspaceRetentionRule).filter_by(
            workflow_id=workflow.id_
        ):
            rule.status = WorkspaceRetentionRuleStatus.inactive
        if retention_rule_states:
            for rule in Session.query(WorkspaceRetentionRule).filter(
                WorkspaceRetentionRule.id_.in_(retention_rule_states)
            ):
                rule.status = retention_rule_states[rule.id_]
        Session.commit()
    except Exception:
        Session.rollback()
        logging.exception("Could not compensate failed restart %s.", workflow.id_)
    try:
        _recalculate_shared_workspace_quota(workflow, user)
    except Exception:
        Session.rollback()
        logging.exception("Could not recalculate quota after failed restart.")


def _enforce_restart_spec_constraints(workflow, new_spec, source_root=None):
    """Enforce the narrowed restart contract for a replacement specification.

    A restart reuses the workflow *source* files already present in the
    workspace; ``-f``/``--file`` only replaces the *specification* (input
    parameters, operational options, workflow type/definition metadata).
    Staging a replacement ``reana.yaml`` does not bring new source files into
    the workspace, so every workflow-source file the replacement references
    (``workflow.file`` and any ``workflow.files`` entries) must already exist in
    the workspace. Keeping the referenced source current -- including uploading
    a new ``workflow.file`` to the workspace before restarting -- is the user's
    responsibility. Changing ``workflow.file`` is allowed as long as the new
    source is present in the workspace.

    Serial specifications carry their steps inline in
    ``workflow.specification`` (they have no ``workflow.file``) and are skipped.

    :raises REANAValidationError: if the replacement spec references a
        workflow-source file missing from the workspace (surfaced to the client
        as HTTP 400).
    """
    source_root = source_root or workflow.workspace_path
    new_workflow = new_spec.get("workflow", {}) or {}
    if not isinstance(new_workflow, dict):
        return
    new_file = new_workflow.get("file")
    if not new_file:
        # Serial (inline specification) or fileless spec: nothing to enforce.
        return

    referenced_main_file = (
        new_file.partition("#")[0]
        if new_workflow.get("type") == "cwl" and isinstance(new_file, str)
        else new_file
    )
    referenced_files = [referenced_main_file] + list(
        new_workflow.get("files", []) or []
    )
    workflow_parameters = new_workflow.get("parameters") or {}
    if isinstance(workflow_parameters, dict) and workflow_parameters.get("file"):
        referenced_files.append(workflow_parameters["file"])
    inputs = new_spec.get("inputs") or {}
    inputs_parameters = (
        inputs.get("parameters") or {} if isinstance(inputs, dict) else {}
    )
    if (
        new_workflow.get("type") in ("cwl", "snakemake")
        and isinstance(inputs_parameters, dict)
        and inputs_parameters.get("input")
    ):
        referenced_files.append(inputs_parameters["input"])
    for referenced in referenced_files:
        if not _workspace_has_source_file(source_root, referenced):
            raise REANAValidationError(
                "Workflow source file '{file}' referenced by the restart "
                "specification is not present in the workspace. A restart "
                "reuses the workflow source files already in the workspace; "
                "to run different source, create a new workflow.".format(
                    file=referenced
                )
            )
    for referenced in list(new_workflow.get("directories", []) or []):
        directory = os.path.abspath(os.path.join(source_root, referenced))
        workspace = os.path.abspath(source_root)
        if (
            os.path.commonpath([workspace, directory]) != workspace
            or not os.path.isdir(directory)
            or os.path.islink(directory)
        ):
            raise REANAValidationError(
                "Workflow source directory '{directory}' referenced by the "
                "restart specification is not present in the workspace.".format(
                    directory=referenced
                )
            )


def _restart_specification_path_error(error):
    """Return a stable restart diagnostic from typed path-error attributes."""
    path = error.path
    if error.reason == "max_length":
        return REANAValidationError(
            "Workflow source path in {} exceeds {} encoded bytes.".format(
                error.field, SPECIFICATION_BUNDLE_MAX_PATH_BYTES
            )
        )
    reason_messages = {
        "missing": (
            "Workflow source path '{}' referenced by the restart specification "
            "is not present in the workspace."
        ),
        "unsafe": (
            "Workflow source path '{}' referenced by the restart specification "
            "is unsafe."
        ),
        "symlink": (
            "Workflow source path '{}' referenced by the restart specification "
            "is a symbolic link; only regular workspace files and directories "
            "are allowed."
        ),
        "wrong_type": (
            "Workflow source path '{}' referenced by the restart specification "
            "does not have the required regular file or directory type."
        ),
        "conflict": (
            "Workflow source path '{}' has conflicting declarations in the "
            "restart specification."
        ),
        "max_depth": (
            "Workflow source path '{}' referenced by the restart specification "
            "exceeds the maximum path depth."
        ),
        "max_length": (
            "Workflow source path '{}' referenced by the restart specification "
            "exceeds the maximum path length."
        ),
        "unreadable": (
            "Workflow source path '{}' referenced by the restart specification "
            "could not be read securely."
        ),
        "changed": (
            "Workflow source path '{}' referenced by the restart specification "
            "changed while it was being read."
        ),
    }
    template = reason_messages.get(
        error.reason,
        "Workflow source path '{}' referenced by the restart specification "
        "could not be used safely (reason: {}).",
    )
    if error.reason in reason_messages:
        message = template.format(path)
    else:
        message = template.format(path, error.reason)
    return REANAValidationError(message)


def _submit_workflow_locked(workflow, user, **parameters):  # noqa: C901
    """Validate and submit a workflow while its workspace lock is held."""
    operational_options = parameters.get("operational_options", {})
    input_parameters = parameters.get("input_parameters", {})
    restart = parameters.get("restart", False)
    replacement_specification_path = parameters.pop(
        "_replacement_specification_path", None
    )
    staged_restart_directory = None
    restart_promotion = None
    cloned_restart_workflow = None
    retention_rule_states = None
    restart_completed = False

    try:
        validate_operational_options(workflow.type_, operational_options)

        validation_warnings = []
        restart_spec_validated = False
        if restart:
            if workflow.status not in [RunStatus.finished, RunStatus.failed]:
                raise ValueError("Only finished or failed workflows can be restarted.")
            if workflow.workspace_has_pending_retention_rules():
                raise ValueError(
                    "The workflow cannot be restarted because some retention rules are "
                    "currently being applied to the workspace. Please retry later."
                )
            if replacement_specification_path:
                retention_rule_states = _workspace_retention_rule_states(
                    workflow.workspace_path
                )
                try:
                    staged_restart_directory = _stage_workspace_validation_snapshot(
                        workflow.workspace_path,
                        specification_path=replacement_specification_path,
                    )
                except REANASpecificationPathError as error:
                    raise _restart_specification_path_error(error) from error
                workspace_specification = load_raw_spec_mapping(
                    staged_restart_directory
                )
                if not workspace_specification:
                    raise REANAValidationError(
                        "A supplied restart specification must be a non-empty "
                        "YAML mapping."
                    )
                restart_type = workspace_specification.get("workflow", {}).get("type")
                _enforce_restart_spec_constraints(
                    workflow,
                    workspace_specification,
                    source_root=staged_restart_directory,
                )
                reana_specification, validation_warnings = load_and_validate_spec(
                    staged_restart_directory
                )
                restart_spec_validated = True
                workflow = clone_workflow(
                    workflow, reana_specification, restart_type, validate_spec=False
                )
                cloned_restart_workflow = workflow
            else:
                retention_rule_states = _workspace_retention_rule_states(
                    workflow.workspace_path
                )
                workflow = clone_workflow(workflow, None, None)
                cloned_restart_workflow = workflow
        elif workflow.status != RunStatus.created:
            raise ValueError(
                "Workflow {} is already {} and cannot be started "
                "again.".format(workflow.get_full_workflow_name(), workflow.status.name)
            )
        # Binding validation gate. The workspace is the source of truth (A1) and
        # is mutable, so when it carries a reana.yaml we re-load + re-validate it
        # *now* -- right before queueing -- and refresh the stored specification
        # from the loaded result. Loading runs in the sandboxed validator for
        # Snakemake/CWL/Yadage (never in-process) and in-process for serial. This
        # binds what runs to what was validated; an invalid workspace fails the
        # start and the workflow keeps its current status (nothing is queued).
        #
        # Two cases deliberately do NOT re-load from the workspace and instead
        # validate the stored authoritative specification in-process (pure, no
        # code execution -- safe and the only sound option, since an
        # already-serialized spec cannot be round-tripped through the engine
        # loaders):
        #  * plain restart -- clone_workflow already validated the stored spec;
        #    replacement restarts were handled above before cloning from the
        #    staged raw specification; and
        #  * a workspace with no reana.yaml -- launched workflows have it
        #    stripped by filter_input_files and pre-seeding (legacy) workflows
        #    never had it, yet the stored spec is a valid, vetted artifact.
        # The (pure) policy validator is the authoritative check in every branch,
        # and runtime per-job policy is independently re-enforced regardless.
        if restart and restart_spec_validated:
            pass
        elif restart:
            validation_warnings = validate_loaded_spec(workflow.reana_specification)
        elif has_reana_spec_file(workflow.workspace_path):
            (
                workflow.reana_specification,
                validation_warnings,
            ) = _load_and_validate_workspace_snapshot(workflow.workspace_path)
        else:
            validation_warnings = validate_loaded_spec(workflow.reana_specification)
        original_parameters = workflow.reana_specification.get("inputs", {}).get(
            "parameters", {}
        )
        validate_input_parameters(input_parameters, original_parameters)
        Session.object_session(workflow).commit()

        # Backfill the Dask service row for legacy workflows created before this fix
        if ensure_dask_service(workflow):
            Session.object_session(workflow).commit()

        # when starting the workflow, the scheduler will call RWC's
        # `set_workflow_status` with this payload. Drop server-only fields that
        # RWC does not accept in its start schema.
        submission_parameters = dict(parameters)
        submission_parameters.pop("reana_specification", None)
        if staged_restart_directory:
            restart_promotion = _promote_restart_specification(
                workflow.workspace_path, staged_restart_directory
            )
        if cloned_restart_workflow:
            _recalculate_shared_workspace_quota(cloned_restart_workflow, user)
        publish_workflow_submission(workflow, user.id_, submission_parameters)
        if restart_promotion:
            _complete_restart_specification_promotion(restart_promotion)
            restart_promotion = None
        restart_completed = True
        response = {
            "message": "Workflow submitted.",
            "workflow_id": workflow.id_,
            "workflow_name": workflow.name,
            "status": RunStatus.queued.name,
            "run_number": workflow.run_number,
            "user": str(user.id_),
        }
        if validation_warnings:
            response["validation_warnings"] = validation_warnings
        return response, 200
    except HTTPError as e:
        if e.response.status_code >= 500:
            return (
                _internal_error_response("Controller error while starting a workflow."),
                e.response.status_code,
            )
        logging.error(traceback.format_exc())
        return e.response.json(), e.response.status_code
    except (REANAValidationError, ValidationError) as e:
        logging.error(traceback.format_exc())
        return {"message": str(e)}, 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return {"message": str(e)}, 403
    except Exception:
        return _internal_error_response("Unexpected error starting a workflow."), 500
    finally:
        if restart_promotion:
            try:
                _rollback_restart_specification(restart_promotion)
            except OSError:
                logging.exception("Could not roll back restart specification.")
        if cloned_restart_workflow and not restart_completed:
            _compensate_failed_restart(
                cloned_restart_workflow, retention_rule_states or {}, user
            )
        if staged_restart_directory:
            shutil.rmtree(staged_restart_directory, ignore_errors=True)


def _submit_workflow(workflow_id_or_name, user, _resolved_workflow=None, **parameters):
    """Resolve, serialize, and submit a workflow through one application boundary."""
    try:
        if user.has_exceeded_quota():
            raise REANAQuotaExceededError(get_quota_excess_message(user))
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")
        workflow = _resolved_workflow or _get_workflow_with_uuid_or_name(
            workflow_id_or_name, str(user.id_)
        )
        with _workspace_mutation_lock(workflow.workspace_path):
            # The identifier must be resolved before taking the workspace lock,
            # but submission decisions must use state refreshed while holding it.
            #
            # KNOWN LIMITATION (tracked in
            # https://github.com/reanahub/reana-workflow-validator/issues/9):
            # for a non-serial workflow whose
            # workspace carries a reana.yaml, _submit_workflow_locked performs the
            # synchronous sandbox validation *inside* this lock. Because the
            # advisory lock is transaction-scoped on the dedicated lock engine,
            # that transaction (and its backend connection) stays open for the
            # whole sandbox wait. Moving validation off the lock-hold path is the
            # proper fix and is deferred to the async-validation work.
            Session.object_session(workflow).refresh(workflow)
            return _submit_workflow_locked(workflow, user, **parameters)
    except WorkspaceMutationConflict:
        return {"message": "The workflow workspace is currently being modified."}, 409
    except WorkspaceMutationUnavailable:
        return (
            _internal_error_response(
                "Workspace mutation serialization is unavailable."
            ),
            503,
        )
    except REANAQuotaExceededError as error:
        return {"message": error.message}, 403
    except ValueError as error:
        logging.error(traceback.format_exc())
        return {"message": str(error)}, 403
    except Exception:
        return (
            _internal_error_response("Unexpected error resolving workflow submission."),
            500,
        )


def _deletion_workspace_paths(workflow_id_or_name, user, all_runs=False):
    """Resolve every workspace a status-deleted operation can mutate."""
    try:
        workflow = _get_workflow_with_uuid_or_name(workflow_id_or_name, str(user.id_))
    except ValueError:
        # Preserve the existing proxy contract for unknown identifiers; RWC
        # remains authoritative for the eventual not-found response.
        return ["workflow-id-or-name:{}".format(workflow_id_or_name)]
    workflows = [workflow]
    if all_runs:
        workflows.extend(
            Session.query(Workflow)
            .filter(
                Workflow.name == workflow.name,
                Workflow.owner_id == workflow.owner_id,
            )
            .all()
        )
    return [candidate.workspace_path for candidate in workflows]


@blueprint.route("/workflows/<workflow_id_or_name>/restart", methods=["POST"])
@signin_required()
@check_quota
def restart_workflow(workflow_id_or_name, user):
    r"""Restart a workflow using one raw replacement specification.

    ---
    post:
      summary: Restart a workflow with a replacement specification.
      description: >-
        Atomically validates and applies one raw replacement REANA specification.
        Workflow source files are reused from the existing workspace.
      operationId: restart_workflow
      consumes:
        - multipart/form-data
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          required: true
          type: string
        - name: access_token
          in: query
          required: false
          type: string
        - name: replacement
          in: formData
          required: true
          type: file
          description: Raw replacement REANA specification.
        - name: parameters
          in: formData
          required: false
          type: string
          description: >-
            JSON object containing optional input_parameters and
            operational_options objects.
      responses:
        200:
          description: Workflow restart was submitted.
          schema:
            $ref: '#/definitions/WorkflowSubmissionResponse'
        400:
          description: Replacement or parameters are malformed or invalid.
          schema:
            $ref: '#/definitions/ErrorResponse'
        401:
          description: Request malformed or missing access token.
          schema:
            $ref: '#/definitions/ErrorResponse'
        403:
          description: Restart is not permitted.
          schema:
            $ref: '#/definitions/ErrorResponse'
        404:
          description: Workflow does not exist.
          schema:
            $ref: '#/definitions/ErrorResponse'
        409:
          description: The workspace is currently being mutated.
          schema:
            $ref: '#/definitions/ErrorResponse'
        413:
          description: Replacement exceeds the configured request limit.
          schema:
            $ref: '#/definitions/ErrorResponse'
        429:
          description: Request rate limit exceeded.
          schema:
            $ref: '#/definitions/ErrorResponse'
        503:
          description: Workspace mutation serialization is unavailable.
          schema:
            $ref: '#/definitions/ErrorResponse'
        500:
          description: Internal server error.
          schema:
            $ref: '#/definitions/ErrorResponse'
    """
    request_directory = None
    try:
        _validate_spec_bundle_request_size()
        _cap_bundle_request_body(max_form_parts=2)
        replacements = request.files.getlist("replacement")
        if set(request.files) != {"replacement"} or len(replacements) != 1:
            return jsonify({"message": "Upload exactly one replacement file."}), 400

        raw_parameters = request.form.get("parameters", "{}")
        try:
            parameters = json.loads(raw_parameters)
        except (TypeError, ValueError):
            return jsonify({"message": "parameters must be a JSON object."}), 400
        if not isinstance(parameters, dict):
            return jsonify({"message": "parameters must be a JSON object."}), 400
        unknown = set(parameters) - {"input_parameters", "operational_options"}
        if unknown:
            return (
                jsonify(
                    {
                        "message": "Unknown restart parameters: {}.".format(
                            ", ".join(sorted(unknown))
                        )
                    }
                ),
                400,
            )
        for name in ("input_parameters", "operational_options"):
            if name in parameters and not isinstance(parameters[name], dict):
                return jsonify({"message": "{} must be an object.".format(name)}), 400

        replacement_stream = replacements[0].stream
        replacement_size = _remaining_stream_size(replacement_stream)
        if replacement_size > REANA_SPEC_BUNDLE_MAX_BYTES:
            raise RequestEntityTooLarge(
                description="Restart specification is too large (maximum is {} bytes).".format(
                    REANA_SPEC_BUNDLE_MAX_BYTES
                )
            )
        request_root = os.path.join(SHARED_VOLUME_PATH, "validation-tmp")
        os.makedirs(request_root, mode=0o700, exist_ok=True)
        request_directory = tempfile.mkdtemp(prefix="restart-", dir=request_root)
        replacement_path = os.path.join(request_directory, "reana.yaml")
        with open(replacement_path, "xb") as destination:
            while True:
                chunk = replacement_stream.read(_FILE_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                destination.write(chunk)

        source_workflow = _get_workflow_with_uuid_or_name(
            workflow_id_or_name, str(user.id_)
        )
        response, status_code = _submit_workflow(
            workflow_id_or_name,
            user,
            _resolved_workflow=source_workflow,
            restart=True,
            _replacement_specification_path=replacement_path,
            **parameters,
        )
        return jsonify(response), status_code
    except RequestEntityTooLarge as error:
        return jsonify({"message": error.description}), 413
    except ValueError as error:
        return jsonify({"message": str(error)}), 404
    finally:
        if request_directory:
            shutil.rmtree(request_directory, ignore_errors=True)


@blueprint.route("/workflows/<workflow_id_or_name>/start", methods=["POST"])
@signin_required()
@use_kwargs(
    {
        "operational_options": fields.Dict(),
        "input_parameters": fields.Dict(),
        "restart": fields.Boolean(),
        # Accepted by the parser only so a released client's replacement restart
        # is answered with an actionable upgrade message instead of an unknown-
        # field complaint. Deliberately absent from the OpenAPI contract: it is
        # rejected in the view and is not part of the supported request.
        "reana_specification": fields.Raw(),
    },
    location="json",
)
@check_quota
def start_workflow(workflow_id_or_name, user, **parameters):  # noqa
    r"""Start workflow.
    ---
    post:
      summary: Start workflow.
      description: >-
        This resource starts the workflow execution process.
        Resource is expecting a workflow UUID.


        The workspace is the authoritative copy of the specification: before the
        workflow is queued, the server re-loads and re-validates the
        specification *from the current workspace* (in a sandbox for
        Snakemake/CWL/Yadage, in-process for serial) and refreshes the stored
        specification from it. A workspace that no longer loads or fails policy
        is rejected with a 400 and the workflow keeps its current status. Any
        non-blocking validation findings are returned in ``validation_warnings``.
      operationId: start_workflow
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: parameters
          in: body
          description: >-
            Optional. Additional input parameters and operational options.
          required: false
          schema:
            type: object
            properties:
              operational_options:
                description: Optional. Additional operational options for workflow execution.
                type: object
              input_parameters:
                description: >-
                  Optional. Additional input parameters that override the ones from
                  the workflow specification.
                type: object
              restart:
                description: Optional. If true, restart the given workflow.
                type: boolean
      responses:
        200:
          description: >-
            Request succeeded. Info about a workflow, including the execution
            status is returned.
          schema:
            $ref: '#/definitions/WorkflowSubmissionResponse'
          examples:
            application/json:
              {
                "message": "Workflow submitted.",
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest",
                "run_number": "1",
                "status": "queued",
                "user": "00000000-0000-0000-0000-000000000000"
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either User or Workflow does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1
                            does not exist"
              }
        409:
          description: >-
            Request failed. The workflow could not be started due to a
            conflict.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1
                            could not be started because it is already
                            running."
              }
        429:
          description: Request rate limit exceeded.
          schema:
            type: object
            required:
              - message
            properties:
              message:
                type: string
        503:
          description: Workspace mutation serialization is unavailable.
          schema:
            $ref: '#/definitions/ErrorResponse'
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "An internal server error occurred."
              }
        501:
          description: >-
            Request failed. The specified status change is not implemented.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Status resume is not supported yet."
              }
    """
    if "reana_specification" in parameters:
        return (
            jsonify(
                {
                    "message": _legacy_specification_message(
                        "A restart that replaces the specification now uploads the "
                        "raw specification to the multipart "
                        "'/api/workflows/{id}/restart' operation."
                    )
                }
            ),
            400,
        )
    response, status_code = _submit_workflow(workflow_id_or_name, user, **parameters)
    return jsonify(response), status_code


@blueprint.route("/workflows/<workflow_id_or_name>/status", methods=["PUT"])
@signin_required()
@use_kwargs(
    {"status": fields.Str(required=True)}, location="query", unknown=marshmallow.EXCLUDE
)
@use_kwargs(
    {
        # parameters for "start"
        "input_parameters": fields.Dict(),
        "operational_options": fields.Dict(),
        "restart": fields.Boolean(),
        # parameters for "deleted"
        "all_runs": fields.Boolean(),
        "workspace": fields.Boolean(),
    },
    location="json",
)
def set_workflow_status(workflow_id_or_name, user, status, **parameters):  # noqa
    r"""Set workflow status.
    ---
    put:
      summary: Set status of a workflow.
      description: >-
        This resource reports the status of a workflow.
        Resource is expecting a workflow UUID.
      operationId: set_workflow_status
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: status
          in: query
          description: >-
            Required. New workflow status. The `start` value is retained for
            compatibility; new clients should use
            POST /api/workflows/<workflow_id_or_name>/start.
          required: true
          type: string
          enum:
            - start
            - stop
            - deleted
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: parameters
          in: body
          description: >-
            Optional. Additional parameters to customise the workflow status change.
          required: false
          schema:
            type: object
            properties:
              input_parameters:
                description: >-
                  Optional. Additional input parameters that override the ones
                  from the workflow specification. Only allowed when status is
                  `start`.
                type: object
              operational_options:
                description: >-
                  Optional. Additional operational options for workflow
                  execution. Only allowed when status is `start`.
                type: object
              restart:
                description: >-
                  Optional. If true, restart the given workflow. Only allowed
                  when status is `start`.
                type: boolean
              all_runs:
                description: >-
                  Optional. If true, delete all runs of the workflow.
                  Only allowed when status is `deleted`.
                type: boolean
              workspace:
                description: >-
                  Optional, but must be set to true if provided.
                  If true, delete also the workspace of the workflow.
                  Only allowed when status is `deleted`.
                type: boolean
      responses:
        200:
          description: >-
            Request succeeded. Info about a workflow, including the status is
            returned.
          schema:
            $ref: '#/definitions/WorkflowSubmissionResponse'
          examples:
            application/json:
              {
                "message": "Workflow successfully launched",
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest",
                "status": "created",
                "user": "00000000-0000-0000-0000-000000000000"
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either User or Workflow does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1
                            does not exist"
              }
        409:
          description: >-
            Request failed. The workflow could not be started due to a
            conflict.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1
                            could not be started because it is already
                            running."
              }
        429:
          description: Request rate limit exceeded.
          schema:
            type: object
            required:
              - message
            properties:
              message:
                type: string
        503:
          description: Workspace mutation serialization is unavailable.
          schema:
            $ref: '#/definitions/ErrorResponse'
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
        501:
          description: >-
            Request failed. The specified status change is not implemented.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Status resume is not supported yet."
              }
    """
    try:
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        if status == "start":
            response, status_code = _submit_workflow(
                workflow_id_or_name, user, **parameters
            )
            # Preserve the legacy response shape of this endpoint.
            response.pop("run_number", None)
            return jsonify(response), status_code

        parameters = request.json if request.is_json else None

        def set_controller_status():
            return current_rwc_api_client.api.set_workflow_status(
                user=str(user.id_),
                workflow_id_or_name=workflow_id_or_name,
                status=status,
                parameters=parameters,
                _request_options=_RWC_MUTATION_REQUEST_OPTIONS,
            ).result()

        if status == "deleted":
            all_runs = (parameters or {}).get("all_runs", False)
            if all_runs:
                try:
                    workflow = _get_workflow_with_uuid_or_name(
                        workflow_id_or_name, str(user.id_)
                    )
                except ValueError:
                    lock_paths = _deletion_workspace_paths(
                        workflow_id_or_name, user, all_runs=True
                    )
                    with workspace_mutation_locks(lock_paths):
                        response, http_response = set_controller_status()
                else:
                    with workflow_family_mutation_lock(
                        workflow.owner_id, workflow.name
                    ):
                        # Creation takes the family lock before its row becomes
                        # visible. Once held, this query is the closed set of
                        # workspaces the controller can select, including a run
                        # that becomes terminal before controller deletion.
                        lock_paths = _deletion_workspace_paths(
                            workflow_id_or_name, user, all_runs=True
                        )
                        with workspace_mutation_locks(lock_paths):
                            response, http_response = set_controller_status()
            else:
                lock_paths = _deletion_workspace_paths(
                    workflow_id_or_name, user, all_runs=False
                )
                with workspace_mutation_locks(lock_paths):
                    response, http_response = set_controller_status()
        else:
            response, http_response = set_controller_status()

        return jsonify(response), http_response.status_code
    except WorkspaceMutationConflict:
        return (
            jsonify({"message": "The workflow workspace is currently being modified."}),
            409,
        )
    except WorkspaceMutationUnavailable:
        return (
            jsonify(
                _internal_error_response(
                    "Workspace mutation serialization is unavailable."
                )
            ),
            503,
        )
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except BravadoTimeoutError:
        return jsonify({"message": "Workflow controller request timed out."}), 503
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/workspace", methods=["POST"])
@signin_required()
@check_quota
@_serialize_workspace_mutation
def upload_file(workflow_id_or_name, user):  # noqa
    r"""Upload file to workspace.

    ---
    post:
      summary: Adds a file to the workspace.
      description: >-
        This resource is expecting a file to place in the workspace.
      operationId: upload_file
      consumes:
        - application/octet-stream
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: file
          in: body
          description: Required. File to add to the workspace.
          required: true
          schema:
            type: string
        - name: file_name
          in: query
          description: Required. File name.
          required: true
          type: string
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: preview
          in: query
          description: >-
            Optional flag to return a previewable response of the file
            (corresponding mime-type).
          required: false
          type: boolean
      responses:
        200:
          description: >-
            Request succeeded. File successfully transferred.
          schema:
            type: object
            properties:
              message:
                type: string
        400:
          description: >-
            Request failed. The incoming payload seems malformed
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "No file_name provided"
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist"
              }
        409:
          description: The workspace is currently being mutated.
          schema:
            $ref: '#/definitions/ErrorResponse'
        411:
          description: A Content-Length header is required for streaming uploads.
          schema:
            $ref: '#/definitions/ErrorResponse'
        503:
          description: Workspace mutation or controller service is unavailable.
          schema:
            $ref: '#/definitions/ErrorResponse'
        500:
          description: >-
            Request failed. Internal server error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """

    try:
        filename = request.args.get("file_name")
        if not filename:
            return jsonify({"message": "No file_name provided"}), 400
        if request.mimetype != "application/octet-stream":
            return (
                jsonify(
                    {
                        "message": f"Wrong Content-Type "
                        f'{request.headers.get("Content-Type")} '
                        f"use application/octet-stream"
                    }
                ),
                400,
            )
        if request.content_length is None:
            return jsonify({"message": "Content-Length header is required."}), 411
        upload_stream = request.stream
        upload_size = request.content_length

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        forwarded_stream = RequestStreamWithLen(upload_stream, upload_size)
        prevent_disk_quota_excess(
            user, len(forwarded_stream), action=f"Uploading file {filename}"
        )
        api_url = current_rwc_api_client.swagger_spec.__dict__.get("api_url")
        endpoint = current_rwc_api_client.api.upload_file.operation.path_name.format(
            workflow_id_or_name=workflow_id_or_name
        )
        http_response = requests.post(
            urlparse.urljoin(api_url, endpoint),
            data=forwarded_stream,
            params={"user": str(user.id_), "file_name": request.args.get("file_name")},
            headers={"Content-Type": "application/octet-stream"},
            timeout=(
                RWC_MUTATION_CONNECT_TIMEOUT,
                RWC_MUTATION_READ_TIMEOUT,
            ),
        )
        return jsonify(http_response.json()), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except (REANAQuotaExceededError, ValueError) as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except requests.exceptions.RequestException:
        logging.error(traceback.format_exc())
        return jsonify({"message": "Workflow controller request failed."}), 503
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route(
    "/workflows/<workflow_id_or_name>/workspace/<path:file_name>", methods=["GET"]
)
@signin_required()
def download_file(workflow_id_or_name, file_name, user):  # noqa
    r"""Download a file from the workspace.

    ---
    get:
      summary: Returns the requested file.
      description: >-
        This resource is expecting a workflow UUID and a file name existing
        inside the workspace to return its content.
      operationId: download_file
      produces:
        - application/octet-stream
        - application/json
        - application/zip
        - image/*
        - text/html
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. workflow UUID or name.
          required: true
          type: string
        - name: file_name
          in: path
          description: Required. Name (or path) of the file to be downloaded.
          required: true
          type: string
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
      responses:
        200:
          description: >-
            Requests succeeded. The file has been downloaded.
          schema:
            type: file
          headers:
            Content-Disposition:
              type: string
            Content-Type:
              type: string
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. `file_name` does not exist .
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "input.csv does not exist"
              }
        500:
          description: >-
            Request failed. Internal server error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """
    try:
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")
        preview = request.args.get("preview", False) or False
        api_url = current_rwc_api_client.swagger_spec.__dict__.get("api_url")
        endpoint = current_rwc_api_client.api.download_file.operation.path_name.format(
            workflow_id_or_name=workflow_id_or_name, file_name=file_name
        )
        req = requests.get(
            urlparse.urljoin(api_url, endpoint),
            params={"preview": preview, "user": str(user.id_)},
            stream=True,
        )
        response = Response(
            stream_with_context(req.iter_content(chunk_size=1024)),
            content_type=req.headers["Content-Type"],
        )
        if req.headers.get("Content-Disposition"):
            response.headers["Content-Disposition"] = req.headers.get(
                "Content-Disposition"
            )
        return response, req.status_code

    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route(
    "/workflows/<workflow_id_or_name>/workspace/<path:file_name>", methods=["DELETE"]
)
@signin_required()
@_serialize_workspace_mutation
def delete_file(workflow_id_or_name, file_name, user):  # noqa
    r"""Delete a file from the workspace.

    ---
    delete:
      summary: Delete the specified file.
      description: >-
        This resource is expecting a workflow UUID and a filename existing
        inside the workspace to be deleted.
      operationId: delete_file
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Workflow UUID or name
          required: true
          type: string
        - name: file_name
          in: path
          description: Required. Name (or path) of the file to be deleted.
          required: true
          type: string
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Details about deleted files and failed deletions are returned.
          schema:
            type: object
            properties:
              deleted:
                type: object
                additionalProperties:
                  type: object
                  properties:
                    size:
                      type: integer
              failed:
                type: object
                additionalProperties:
                  type: object
                  properties:
                    error:
                      type: string
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. `file_name` does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "input.csv does not exist"
              }
        409:
          description: The workspace is currently being mutated.
          schema:
            $ref: '#/definitions/ErrorResponse'
        503:
          description: Workspace mutation serialization is unavailable.
          schema:
            $ref: '#/definitions/ErrorResponse'
        500:
          description: >-
            Request failed. Internal server error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """
    try:
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.delete_file(
            user=str(user.id_),
            workflow_id_or_name=workflow_id_or_name,
            file_name=file_name,
            _request_options=_RWC_MUTATION_REQUEST_OPTIONS,
        ).result()

        return jsonify(http_response.json()), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except BravadoTimeoutError:
        return jsonify({"message": "Workflow controller request timed out."}), 503
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/workspace", methods=["GET"])
@use_kwargs(
    {
        "file_name": fields.String(),
        "page": fields.Int(validate=validate.Range(min=1)),
        "size": fields.Int(validate=validate.Range(min=1)),
        "search": fields.String(),
    },
    location="query",
    unknown=marshmallow.EXCLUDE,
)
@signin_required()
def get_files(workflow_id_or_name, user, **kwargs):  # noqa
    r"""List all files contained in a workspace.

    ---
    get:
      summary: Returns the workspace file list.
      description: >-
        This resource retrieves the file list of a workspace, given
        its workflow UUID.
      operationId: get_files
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: file_name
          in: query
          description: File name(s) (glob) to list.
          required: false
          type: string
        - name: page
          in: query
          description: Results page number (pagination).
          required: false
          type: integer
        - name: size
          in: query
          description: Number of results per page (pagination).
          required: false
          type: integer
        - name: search
          in: query
          description: Filter workflow workspace files by file name, size, or modification date.
          required: false
          type: string
      responses:
        200:
          description: >-
            Requests succeeded. The list of files has been retrieved.
          schema:
            type: object
            properties:
              total:
                type: integer
              items:
                type: array
                items:
                  type: object
                  properties:
                    name:
                      type: string
                    last-modified:
                      type: string
                    size:
                      type: object
                      properties:
                        raw:
                          type: integer
                        human_readable:
                          type: string
        400:
          description: >-
            Request failed. The request parameters are invalid or the filtered
            result set exceeds the configured display limit.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Too many files to display (e.g. limit=100000).
                            Please use more specific filters to narrow the
                            results. Available filters: file name, size, or
                            last-modified."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Analysis does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Analysis 256b25f4-4cfb-4684-b7a8-73872ef455a1 does
                            not exist."
              }
        500:
          description: >-
            Request failed. Internal server error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """
    try:
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.get_files(
            user=str(user.id_),
            workflow_id_or_name=workflow_id_or_name,
            **kwargs,
        ).result()

        return jsonify(http_response.json()), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/parameters", methods=["GET"])
@signin_required()
def get_workflow_parameters(workflow_id_or_name, user):  # noqa
    r"""Get workflow input parameters.

    ---
    get:
      summary: Get parameters of a workflow.
      description: >-
        This resource reports the input parameters of a workflow.
        Resource is expecting a workflow UUID.
      operationId: get_workflow_parameters
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Workflow input parameters, including the status
            are returned.
          schema:
            type: object
            properties:
              id:
                type: string
              name:
                type: string
              type:
                type: string
              parameters:
                type: object
                minProperties: 0
          examples:
            application/json:
              {
                'id': 'dd4e93cf-e6d0-4714-a601-301ed97eec60',
                'name': 'workflow.24',
                'type': 'serial',
                'parameters': {'helloworld': 'code/helloworld.py',
                               'inputfile': 'data/names.txt',
                               'outputfile': 'results/greetings.txt',
                               'sleeptime': 2}
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either User or Analysis does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Analysis 256b25f4-4cfb-4684-b7a8-73872ef455a1 does
                            not exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.get_workflow_parameters(
            user=str(user.id_), workflow_id_or_name=workflow_id_or_name
        ).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route(
    "/workflows/<workflow_id_or_name_a>/diff/" "<workflow_id_or_name_b>",
    methods=["GET"],
)
@signin_required()
def get_workflow_diff(workflow_id_or_name_a, workflow_id_or_name_b, user):  # noqa
    r"""Get differences between two workflows.

    ---
    get:
      summary: Get diff between two workflows.
      description: >-
        This resource shows the differences between
        the assets of two workflows.
        Resource is expecting two workflow UUIDs or names.
      operationId: get_workflow_diff
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name_a
          in: path
          description: Required. Analysis UUID or name of the first workflow.
          required: true
          type: string
        - name: workflow_id_or_name_b
          in: path
          description: Required. Analysis UUID or name of the second workflow.
          required: true
          type: string
        - name: brief
          in: query
          description: Optional flag. If set, file contents are examined.
          required: false
          type: boolean
          default: false
        - name: context_lines
          in: query
          description: Optional parameter. Sets number of context lines
                       for workspace diff output.
          required: false
          type: string
          default: '5'
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Info about a workflow, including the status is
            returned.
          schema:
            type: object
            properties:
              reana_specification:
                type: string
              workspace_listing:
                type: string
          examples:
            application/json:
              {
                "reana_specification":
                ["- nevents: 100000\n+ nevents: 200000"],
                "workspace_listing": {"Only in workspace a: code"}
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either user or workflow does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1 does
                            not exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        brief = json.loads(request.args.get("brief", "false").lower())
        context_lines = request.args.get("context_lines", 5)
        if not workflow_id_or_name_a or not workflow_id_or_name_b:
            raise ValueError("Workflow id or name is not supplied")

        response, http_response = current_rwc_api_client.api.get_workflow_diff(
            user=str(user.id_),
            brief=brief,
            context_lines=context_lines,
            workflow_id_or_name_a=workflow_id_or_name_a,
            workflow_id_or_name_b=workflow_id_or_name_b,
        ).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except json.JSONDecodeError:
        logging.error(traceback.format_exc())
        return jsonify({"message": "Your request contains not valid JSON."}), 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route(
    "/workflows/<workflow_id_or_name>/open/" "<interactive_session_type>",
    methods=["POST"],
)
@signin_required()
@check_quota
def open_interactive_session(
    workflow_id_or_name, interactive_session_type, user
):  # noqa
    r"""Start an interactive session inside the workflow workspace.

    ---
    post:
      summary: Start an interactive session inside the workflow workspace.
      description: >-
        This resource is expecting a workflow to start an interactive session
        within its workspace.
      operationId: open_interactive_session
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Workflow UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: interactive_session_type
          in: path
          description: Type of interactive session to use.
          required: true
          type: string
        - name: interactive_session_configuration
          in: body
          description: >-
            Interactive session configuration.
          required: false
          schema:
            type: object
            properties:
              image:
                type: string
                description: >-
                  Replaces the default Docker image of an interactive session.
      responses:
        200:
          description: >-
            Request succeeded. The interactive session has been opened.
          schema:
            type: object
            properties:
              path:
                type: string
          examples:
            application/json:
              {
                "path": "/dd4e93cf-e6d0-4714-a601-301ed97eec60",
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either user or workflow does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Interactive session type jupiter not found, try
                            with one of: [jupyter]."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        if interactive_session_type not in InteractiveSessionType.__members__:
            return (
                jsonify(
                    {
                        "message": "Interactive session type {0} not found, try "
                        "with one of: {1}".format(
                            interactive_session_type,
                            [e.name for e in InteractiveSessionType],
                        )
                    }
                ),
                404,
            )
        if not workflow_id_or_name:
            raise KeyError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.open_interactive_session(
            user=str(user.id_),
            workflow_id_or_name=workflow_id_or_name,
            interactive_session_type=interactive_session_type,
            interactive_session_configuration=request.json if request.is_json else None,
        ).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        # Try to parse JSON, but gracefully handle empty/non-JSON responses
        try:
            error_payload = e.response.json()
            return jsonify(error_payload), e.response.status_code
        except ValueError:
            return (
                jsonify(
                    {"message": (f"Workflow '{workflow_id_or_name}' does not exist.")}
                ),
                404,
            )
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/close/", methods=["POST"])
@signin_required()
def close_interactive_session(workflow_id_or_name, user):  # noqa
    r"""Close an interactive workflow session.

    ---
    post:
      summary: Close an interactive workflow session.
      description: >-
        This resource is expecting a workflow to close an interactive session
        within its workspace.
      operationId: close_interactive_session
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Workflow UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
      responses:
        200:
          description: >-
            Request succeeded. The interactive session has been closed.
          schema:
            type: object
            properties:
              path:
                type: string
          examples:
            application/json:
              {
                "message": "The interactive session has been closed",
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either user or workflow does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Either user or workflow does not exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        if not workflow_id_or_name:
            raise KeyError("workflow_id_or_name is not supplied")
        response, http_response = current_rwc_api_client.api.close_interactive_session(
            user=str(user.id_), workflow_id_or_name=workflow_id_or_name
        ).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/move_files/<workflow_id_or_name>", methods=["PUT"])
@signin_required()
@_serialize_workspace_mutation
def move_files(workflow_id_or_name, user):  # noqa
    r"""Move files within workspace.
    ---
    put:
      summary: Move files within workspace.
      description: >-
        This resource moves files within the workspace. Resource is expecting
        a workflow UUID.
      operationId: move_files
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: source
          in: query
          description: Required. Source file(s).
          required: true
          type: string
        - name: target
          in: query
          description: Required. Target file(s).
          required: true
          type: string
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Message about successfully moved files is
            returned.
          schema:
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
          examples:
            application/json:
              {
                "message": "Files were successfully moved",
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either User or Workflow does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1
                            does not exist"
              }
        409:
          description: >-
            Request failed. The files could not be moved due to a conflict.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Path folder/ does not exist"
              }
        503:
          description: Workspace mutation serialization is unavailable.
          schema:
            $ref: '#/definitions/ErrorResponse'
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")
        source = request.args.get("source")
        target = request.args.get("target")
        response, http_response = current_rwc_api_client.api.move_files(
            user=str(user.id_),
            workflow_id_or_name=workflow_id_or_name,
            source=source,
            target=target,
            _request_options=_RWC_MUTATION_REQUEST_OPTIONS,
        ).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except BravadoTimeoutError:
        return jsonify({"message": "Workflow controller request timed out."}), 503
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/disk_usage", methods=["GET"])
@signin_required()
def get_workflow_disk_usage(workflow_id_or_name, user):  # noqa
    r"""Get workflow disk usage.

    ---
    get:
      summary: Get disk usage of a workflow.
      description: >-
        This resource reports the disk usage of a workflow.
        Resource is expecting a workflow UUID and some parameters .
      operationId: get_workflow_disk_usage
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: parameters
          in: body
          description: >-
            Optional. Additional input parameters and operational options.
          required: false
          schema:
            type: object
            properties:
              summarize:
                type: boolean
              search:
                type: string
      responses:
        200:
          description: >-
            Request succeeded. Info about the disk usage is
            returned.
          schema:
            type: object
            properties:
              workflow_id:
                type: string
              workflow_name:
                type: string
              user:
                type: string
              disk_usage_info:
                type: array
                items:
                  type: object
                  properties:
                    name:
                      type: string
                    size:
                      type: object
                      properties:
                        raw:
                          type: integer
                        human_readable:
                          type: string
          examples:
            application/json:
              {
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
                "disk_usage_info": [{'name': 'file1.txt',
                                      'size': {
                                        'raw': 12580000,
                                        'human_readable': '12 MB'
                                       }
                                    },
                                    {'name': 'plot.png',
                                     'size': {
                                       'raw': 184320,
                                       'human_readable': '100 KB'
                                      }
                                    }]
              }
        400:
          description: >-
            Request failed. The incoming data specification seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist"
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        parameters = request.json if request.is_json else {}

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")
        workflow = _get_workflow_with_uuid_or_name(
            workflow_id_or_name, str(user.id_), True
        )
        summarize = bool(parameters.get("summarize", False))
        search = parameters.get("search", None)
        disk_usage_info = workflow.get_workspace_disk_usage(
            summarize=summarize, search=search
        )
        response = {
            "workflow_id": workflow.id_,
            "workflow_name": workflow.name,
            "user": str(user.id_),
            "disk_usage_info": disk_usage_info,
        }

        return jsonify(response), 200
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/retention_rules")
@signin_required()
def get_workflow_retention_rules(workflow_id_or_name, user):
    r"""Get the retention rules of a workflow.

    ---
    get:
      summary: Get the retention rules of a workflow.
      description: >-
        This resource returns all the retention rules of a given workflow.
      operationId: get_workflow_retention_rules
      produces:
       - application/json
      parameters:
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. The response contains the list of all the retention rules.
          schema:
            type: object
            properties:
              workflow_id:
                type: string
              workflow_name:
                type: string
              retention_rules:
                type: array
                items:
                  type: object
                  properties:
                    id:
                      type: string
                    workspace_files:
                      type: string
                    retention_days:
                      type: integer
                    apply_on:
                      type: string
                      x-nullable: true
                    status:
                      type: string
          examples:
            application/json:
              {
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
                "retention_rules": [
                    {
                      "id": "851da5cf-0b26-40c5-97a1-9acdbb35aac7",
                      "workspace_files": "**/*.tmp",
                      "retention_days": 1,
                      "apply_on": "2022-11-24T23:59:59",
                      "status": "active"
                    }
                ]
              }
        401:
          description: >-
            Request failed. User not signed in.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User not signed in."
              }
        403:
          description: >-
            Request failed. Credentials are invalid or revoked.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Token not valid."
              }
        404:
          description: >-
            Request failed. Workflow does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow mytest.1 does not exist."
              }
        500:
          description: >-
            Request failed. Internal server error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Something went wrong."
              }
    """
    try:
        (
            response,
            http_response,
        ) = current_rwc_api_client.api.get_workflow_retention_rules(
            user=str(user.id_),
            workflow_id_or_name=workflow_id_or_name,
        ).result()
        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.exception(str(e))
        return jsonify(e.response.json()), e.response.status_code
    except Exception as e:
        logging.exception(str(e))
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/prune", methods=["POST"])
@use_kwargs(
    {
        "include_inputs": fields.Boolean(),
        "include_outputs": fields.Boolean(),
    },
    location="json",
    unknown=marshmallow.EXCLUDE,
)
@signin_required()
@_serialize_workspace_mutation
def prune_workspace(
    workflow_id_or_name, user, include_inputs=False, include_outputs=False
):
    r"""Prune workspace files.

    ---
    post:
      summary: Prune the workspace's files.
      description: >-
        This resource deletes the workspace's files that are neither
        in the input nor in the output of the workflow definition.
        This resource is expecting a workflow UUID and some parameters.
      operationId: prune_workspace
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: include_inputs
          in: query
          description: >-
            Optional. Delete also the input files of the workflow.
          required: false
          type: boolean
        - name: include_outputs
          in: query
          description: >-
            Optional. Delete also the output files of the workflow.
          required: false
          type: boolean
      responses:
        200:
          description: >-
            Request succeeded. The workspace has been pruned.
          schema:
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
          examples:
            application/json:
              {
                "message": "The workspace has been correctly pruned.",
                "workflow_id": "cdcf48b1-c2f3-4693-8230-b066e088c6ac",
                "workflow_name": "mytest.1"
              }
        409:
          description: The workspace is currently being mutated.
          schema:
            $ref: '#/definitions/ErrorResponse'
        503:
          description: Workspace mutation serialization is unavailable.
          schema:
            $ref: '#/definitions/ErrorResponse'
        400:
          description: >-
            Request failed. The incoming data specification seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist"
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        which_to_keep = InOrOut.INPUTS_OUTPUTS
        if include_inputs:
            which_to_keep = InOrOut.OUTPUTS
        if include_outputs:
            which_to_keep = InOrOut.INPUTS
            if include_inputs:
                which_to_keep = InOrOut.NONE

        workflow = _get_workflow_with_uuid_or_name(workflow_id_or_name, str(user.id_))
        deleter = Deleter(workflow)
        for file_or_dir in workspace.iterdir(deleter.workspace, ""):
            deleter.delete_files(which_to_keep, file_or_dir)
        response = {
            "message": "The workspace has been correctly pruned.",
            "workflow_id": workflow.id_,
            "workflow_name": workflow.name,
        }
        return jsonify(response), 200
    except HTTPError as e:
        logging.exception(str(e))
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        # In case of invalid workflow name / UUID
        logging.exception(str(e))
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.exception(str(e))
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/share", methods=["POST"])
@signin_required()
@use_kwargs(
    {
        "user_email_to_share_with": fields.Str(required=True),
        "message": fields.Str(),
        "valid_until": fields.Str(),
    },
    location="json",
)
def share_workflow(workflow_id_or_name, user, **kwargs):
    r"""Share a workflow with another user.

    ---
    post:
      summary: Share a workflow with another user.
      description: >-
        This resource shares a workflow with another user.
        This resource is expecting a workflow UUID and some parameters.
      operationId: share_workflow
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Workflow UUID or name.
          required: true
          type: string
        - name: share_details
          in: body
          description: JSON object with details of the share.
          required: true
          schema:
            type: object
            properties:
              user_email_to_share_with:
                type: string
                description: User to share the workflow with.
              message:
                type: string
                description: Optional. Message to include when sharing the workflow.
              valid_until:
                type: string
                description: Optional. Date when access to the workflow will expire (format YYYY-MM-DD).
            required: [user_email_to_share_with]
      responses:
        200:
          description: >-
            Request succeeded. The workflow has been shared with the user.
          schema:
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
          examples:
            application/json:
              {
                "message": "The workflow has been shared with the user.",
                "workflow_id": "cdcf48b1-c2f3-4693-8230-b066e088c6ac",
                "workflow_name": "mytest.1"
              }
        400:
          description: >-
            Request failed. The incoming data seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
        401:
          description: >-
            Request failed. User not signed in.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User not signed in."
              }
        403:
          description: >-
            Request failed. Credentials are invalid or revoked.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Token not valid."
              }
        404:
          description: >-
            Request failed. Workflow does not exist or user does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist",
              }
        409:
          description: >-
            Request failed. The workflow is already shared with the user.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "The workflow is already shared with the user.",
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error.",
              }
    """
    try:
        response, http_response = current_rwc_api_client.api.share_workflow(
            workflow_id_or_name=workflow_id_or_name,
            user=str(user.id_),
            share_details=kwargs,
        ).result()

        return jsonify(response), 200
    except HTTPError as e:
        logging.exception(str(e))
        return jsonify(e.response.json()), e.response.status_code
    except Exception as e:
        logging.exception(str(e))
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/unshare", methods=["POST"])
@use_kwargs(
    {
        "user_email_to_unshare_with": fields.String(),
    },
    location="json",
    unknown=marshmallow.EXCLUDE,
)
@signin_required()
def unshare_workflow(workflow_id_or_name, user, user_email_to_unshare_with):
    r"""Unshare a workflow with another user.

    ---
    post:
      summary: Unshare a workflow with another user.
      description: >-
        This resource unshares a workflow with another user.
        This resource is expecting a workflow UUID and some parameters.
      operationId: unshare_workflow
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Workflow UUID or name.
          required: true
          type: string
        - name: user_email_to_unshare_with
          in: query
          description: >-
            Required. User to unshare the workflow with.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. The workflow has been unshared with the user.
          schema:
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
          examples:
            application/json:
              {
                "message": "The workflow has been unshared with the user.",
                "workflow_id": "cdcf48b1-c2f3-4693-8230-b066e088c6ac",
                "workflow_name": "mytest.1"
              }
        400:
          description: >-
            Request failed. The incoming data specification seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to unshare the workflow.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User is not allowed to unshare the workflow."
              }
        404:
          description: >-
            Request failed. Workflow does not exist or user does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist",
              }
        409:
          description: >-
            Request failed. The workflow is not shared with the user.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "The workflow is not shared with the user."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        unshare_params = {
            "workflow_id_or_name": workflow_id_or_name,
            "user_email_to_unshare_with": user_email_to_unshare_with,
            "user": str(user.id_),
        }

        response, http_response = current_rwc_api_client.api.unshare_workflow(
            **unshare_params
        ).result()

        return jsonify(response), 200
    except HTTPError as e:
        logging.exception(str(e))
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        # In case of invalid workflow name / UUID
        logging.exception(str(e))
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.exception(str(e))
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/share-status", methods=["GET"])
@signin_required()
def get_workflow_share_status(workflow_id_or_name, user):
    r"""Get the share status of a workflow.

    ---
    get:
      summary: Get the share status of a workflow.
      description: >-
        This resource returns the share status of a given workflow.
      operationId: get_workflow_share_status
      produces:
       - application/json
      parameters:
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: false
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Workflow UUID or name.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. The response contains the share status of the workflow.
          schema:
            type: object
            properties:
              workflow_id:
                type: string
              workflow_name:
                type: string
              shared_with:
                type: array
                items:
                  type: object
                  properties:
                    user_email:
                      type: string
                    valid_until:
                      type: string
                      x-nullable: true
          examples:
            application/json:
              {
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
                "shared_with": [
                    {
                      "user_email": "bob@example.org",
                      "valid_until": "2022-11-24T23:59:59"
                    }
                ]
              }
        401:
          description: >-
            Request failed. User not signed in.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User not signed in."
              }
        403:
          description: >-
            Request failed. Credentials are invalid or revoked.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Token not valid."
              }
        404:
          description: >-
            Request failed. Workflow does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow mytest.1 does not exist."
              }
        500:
          description: >-
            Request failed. Internal server error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Something went wrong."
              }
    """
    try:
        share_status_params = {
            "workflow_id_or_name": workflow_id_or_name,
            "user": str(user.id_),
        }

        response, http_response = current_rwc_api_client.api.get_workflow_share_status(
            **share_status_params
        ).result()

        return jsonify(response), 200
    except HTTPError as e:
        logging.exception(str(e))
        return jsonify(e.response.json()), e.response.status_code
    except Exception as e:
        logging.exception(str(e))
        return jsonify({"message": str(e)}), 500
