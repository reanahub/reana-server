# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server workflow-functionality Flask-Blueprint."""

import json
import logging
import os
import shutil
import traceback
import uuid

import requests
import yaml
from bravado.exception import HTTPError
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
from reana_commons.errors import REANAQuotaExceededError, REANAValidationError
from reana_commons.validation.operational_options import validate_operational_options
from reana_commons.validation.utils import validate_workflow_name
from reana_db.database import Session
from reana_db.models import InteractiveSessionType, RunStatus
from reana_db.utils import (
    _get_workflow_with_uuid_or_name,
    get_disk_usage_or_zero,
    store_workflow_disk_quota,
    update_users_disk_quota,
)
from reana_server.api_client import current_rwc_api_client
from reana_server.config import (
    REANA_HOSTNAME,
    REANA_SPEC_BUNDLE_MAX_BYTES,
    REANA_SPEC_BUNDLE_MAX_FILES,
)
from reana_server.decorators import check_quota, signin_required
from reana_server.deleter import Deleter, InOrOut
from reana_server.gitlab_client import (
    GitLabClientRequestError,
    GitLabClientInvalidToken,
)
from reana_server.utils import (
    RequestStreamWithLen,
    _fail_gitlab_commit_build_status,
    _get_reana_yaml_from_gitlab,
    clone_workflow,
    ensure_dask_service,
    get_quota_excess_message,
    get_workspace_retention_rules,
    is_uuid_v4,
    mv_workflow_files,
    prevent_disk_quota_excess,
    publish_workflow_submission,
)
from reana_server.validation import (
    REANA_SPEC_FILENAMES,
    check_spec_environments,
    has_reana_spec_file,
    list_spec_images,
    load_and_validate_spec,
    validate_input_parameters,
    validate_loaded_spec,
    validate_spec_bundle,
)
import marshmallow
from webargs import fields, validate
from webargs.flaskparser import use_kwargs

try:
    from urllib import parse as urlparse
except ImportError:
    from urlparse import urlparse

blueprint = Blueprint("workflows", __name__)

VALIDATION_STAGING_SUBDIR = "validation-tmp"

# Chunk size for streaming uploaded bundle members to disk while enforcing the
# size cap (so an oversized member is rejected before it is fully written).
_BUNDLE_CHUNK_SIZE = 1024 * 1024


def _is_truthy_arg(value):
    """Interpret a query-string flag (``?environments=true``) as a boolean."""
    return str(value).lower() in ("1", "true", "yes", "on")


def _validate_spec_bundle_request_size():
    """Reject oversized bundle requests before multipart parsing starts."""
    if (
        request.content_length is not None
        and request.content_length > REANA_SPEC_BUNDLE_MAX_BYTES
    ):
        raise REANAValidationError(
            "Specification bundle is too large (maximum is {} bytes).".format(
                REANA_SPEC_BUNDLE_MAX_BYTES
            )
        )


def _save_member_within_limit(storage, dest, already_written):
    """Stream an uploaded bundle member to ``dest``, enforcing the size cap.

    Werkzeug's ``FileStorage.save`` writes the whole member to disk before its
    size can be checked, so a single very large member (or a chunked upload with
    no ``Content-Length`` to reject up front) is fully written before the cap
    fires. Streaming in bounded chunks and tracking the cumulative total across
    all members lets us abort such an upload *before* it lands on disk.

    :returns: the updated cumulative byte count.
    :raises REANAValidationError: if the cumulative size exceeds the limit.
    """
    total = already_written
    with open(dest, "wb") as out:
        while True:
            chunk = storage.stream.read(_BUNDLE_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > REANA_SPEC_BUNDLE_MAX_BYTES:
                raise REANAValidationError(
                    "Specification bundle is too large (maximum is {} bytes).".format(
                        REANA_SPEC_BUNDLE_MAX_BYTES
                    )
                )
            out.write(chunk)
    return total


def _stage_validation_bundle(files):
    """Stage an uploaded raw spec bundle under the shared volume.

    Each multipart field name is the file path relative to the bundle root.

    :param files: ``request.files`` mapping of relative-path -> uploaded file.
    :returns: ``(abs_dir, rel_path, total_bytes)`` where ``rel_path`` is relative to
        ``SHARED_VOLUME_PATH`` so reana-workflow-controller can mount it as a
        read-only sub-path of the shared volume, and ``total_bytes`` is the
        exact staged bundle size.
    :raises REANAValidationError: on an unsafe (absolute / ``..``) member path,
        too many files, or a bundle that exceeds the configured size limit.
    """
    if len(files) > REANA_SPEC_BUNDLE_MAX_FILES:
        raise REANAValidationError(
            "Specification bundle has too many files (maximum is {}).".format(
                REANA_SPEC_BUNDLE_MAX_FILES
            )
        )
    rel_path = os.path.join(VALIDATION_STAGING_SUBDIR, uuid.uuid4().hex)
    abs_dir = os.path.join(SHARED_VOLUME_PATH, rel_path)
    os.makedirs(abs_dir, exist_ok=True)
    base = os.path.realpath(abs_dir)
    total_bytes = 0
    try:
        for member, storage in files.items():
            if os.path.isabs(member) or ".." in member.replace("\\", "/").split("/"):
                raise REANAValidationError("Unsafe bundle path: {}".format(member))
            dest = os.path.realpath(os.path.join(abs_dir, member))
            if dest != base and not dest.startswith(base + os.sep):
                raise REANAValidationError("Unsafe bundle path: {}".format(member))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # Enforce the size cap while streaming (covers chunked uploads with no
            # Content-Length, which bypass the up-front request-size check).
            total_bytes = _save_member_within_limit(storage, dest, total_bytes)
    except Exception:
        # Never leave a partial bundle behind if staging is rejected midway.
        shutil.rmtree(abs_dir, ignore_errors=True)
        raise
    return abs_dir, rel_path, total_bytes


@blueprint.route("/workflows/validate", methods=["POST"])
@signin_required()
def validate_workflow_specification(user):  # noqa
    r"""Validate a raw REANA workflow specification bundle.

    ---
    post:
      summary: Validate a raw workflow specification bundle.
      description: >-
        Accepts a multipart upload of the raw specification bundle (the
        ``reana.yaml`` plus any referenced workflow/config files, each form field
        named by its path relative to the bundle root). Serial specs are loaded
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
          description: Specification bundle files (reana.yaml plus referenced
            files). Multiple file parts named by their bundle-relative path.
          required: true
          type: file
        - name: access_token
          in: query
          required: false
          type: string
        - name: environments
          in: query
          description: If true, run the cheap (Docker-free) registry checks
            (existence and tag) on the runtime-environment images and return the
            loaded image list plus the cluster runtime UID/GID so the client can
            run the deep checks locally.
          required: false
          type: boolean
        - name: pull
          in: query
          description: Hint that the client will pull and inspect the images
            locally; the server then skips the registry existence lookup (the
            local pull is authoritative) and returns only the image list and
            offline tag warnings.
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
              reana_specification:
                type: object
              errors:
                type: array
                items:
                  type: object
              warnings:
                type: array
                items:
                  type: object
              images:
                description: Distinct runtime images of the loaded spec (only
                  when environments is requested), for client-side checks.
                type: array
                items:
                  type: string
              runtime_uid:
                description: UID REANA runs workflow steps as.
                type: integer
              runtime_gid:
                description: GID REANA runs workflow steps as.
                type: integer
        400:
          description: The bundle was missing or malformed.
        401:
          description: Request malformed or missing access token.
        403:
          description: Request access forbidden.
        500:
          description: Internal error while validating the specification.
    """
    abs_dir = None
    try:
        _validate_spec_bundle_request_size()
        if not request.files:
            return (
                jsonify({"message": "No specification bundle files were provided."}),
                400,
            )
        abs_dir, rel_path, _bundle_bytes = _stage_validation_bundle(request.files)
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
        with open(reana_yaml_path) as f:
            raw_yaml = yaml.safe_load(f) or {}
        workflow_type = raw_yaml.get("workflow", {}).get("type")
        report = validate_spec_bundle(abs_dir, rel_path, workflow_type)
        # Optional runtime-environment (container image) checks, requested by the
        # client via ``--environments``/``--pull``. The server does the cheap,
        # Docker-free part (existence + floating tag) and returns the loaded
        # image list plus the cluster runtime UID/GID so the client can run the
        # deep ``--pull`` checks (pull + inspect) locally. Advisory-only.
        if _is_truthy_arg(request.args.get("environments")) and report.get(
            "reana_specification"
        ):
            local_pull = _is_truthy_arg(request.args.get("pull"))
            report["images"] = list_spec_images(report["reana_specification"])
            report["runtime_uid"] = int(WORKFLOW_RUNTIME_USER_UID)
            report["runtime_gid"] = int(WORKFLOW_RUNTIME_USER_GID)
            # When the client pulls locally it is the authority on existence (and
            # can see private images), so skip the server-side registry lookup
            # and keep only the offline floating-tag warnings.
            report.setdefault("warnings", []).extend(
                check_spec_environments(
                    report["reana_specification"], check_existence=not local_pull
                )
            )
        return jsonify(report), 200
    except REANAValidationError as e:
        return jsonify({"message": str(e)}), 400
    except RequestEntityTooLarge as e:
        return jsonify({"message": str(e)}), 413
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
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
        Creates a workflow from an uploaded specification bundle (multipart
        form data). The bundle contains ``reana.yaml`` plus any referenced
        workflow/parameter files, each form field named by its path relative to
        the bundle root. The server loads and validates the specification
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
          description: Specification bundle files (reana.yaml plus referenced
            files). Multiple file parts named by their bundle-relative path.
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
            Request failed. Not implemented.
    """
    bundle_dir = None
    try:
        if request.args.get("spec"):
            return jsonify("Not implemented"), 501

        request_from_gitlab = request.is_json and "object_kind" in (request.json or {})
        validation_warnings = []
        if request_from_gitlab:
            (
                reana_spec_file,
                git_url,
                workflow_name,
                git_branch,
                git_commit_sha,
            ) = _get_reana_yaml_from_gitlab(request.json, user.id_)
            git_data = {
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
            git_data = {}
            workflow_name = request.args.get("workflow_name", "")
            # Reject an over-quota user *before* any expensive work (staging the
            # bundle on the shared volume and spawning a sandbox validation Job).
            if user.has_exceeded_quota():
                raise REANAQuotaExceededError(get_quota_excess_message(user))
            _validate_spec_bundle_request_size()
            if not request.files:
                raise REANAValidationError(
                    "A workflow specification bundle must be uploaded."
                )
            # Stage the bundle and validate it (B3: create-time lint). Keep it
            # afterwards so it can seed the workspace once the workflow exists
            # (C1); it is removed in the outer ``finally``.
            bundle_dir, _bundle_rel, bundle_bytes = _stage_validation_bundle(
                request.files
            )
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
        # No per-check validation here: the raw-bundle path was already fully
        # validated by load_and_validate_spec above, and GitLab specs are
        # validated post-create from the populated workspace (below).

        retention_days = reana_spec_file.get("workspace", {}).get("retention_days")
        retention_rules = get_workspace_retention_rules(retention_days)

        workflow_dict = {
            "reana_specification": reana_spec_file,
            "workflow_name": workflow_name,
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

        if validation_warnings:
            response["validation_warnings"] = validation_warnings

        if git_data:
            workflow = _get_workflow_with_uuid_or_name(
                response["workflow_id"], str(user.id_)
            )

            # Load + validate the specification from the populated workspace.
            # The workflow files are git-cloned into the workspace by the
            # controller during create, so (unlike the raw-bundle path) the spec
            # can only be loaded + validated here, post-create. Loading runs in
            # the sandboxed validator for Snakemake/CWL/Yadage (never in-process),
            # so GitLab-fetched specs cannot execute code in the API.
            try:
                workflow.reana_specification, gitlab_warnings = load_and_validate_spec(
                    workflow.workspace_path
                )
                gitlab_disk_usage = get_disk_usage_or_zero(
                    workflow.workspace_path, override_policy_checks=True
                )
                prevent_disk_quota_excess(
                    user,
                    gitlab_disk_usage,
                    action=f"Creating the workflow {workflow_name}",
                )
            except Exception:
                # The cloned specification is invalid, the validator service
                # failed, or the cloned repository would exceed disk quota. Mark
                # the just-created workflow deleted and remove its workspace so
                # it does not linger as an unstartable/orphaned run consuming
                # persistent storage.
                workflow.status = RunStatus.deleted
                Session.commit()
                shutil.rmtree(workflow.workspace_path, ignore_errors=True)
                raise
            Session.commit()
            if gitlab_warnings:
                response["validation_warnings"] = gitlab_warnings
            store_workflow_disk_quota(workflow, bytes_to_sum=gitlab_disk_usage)
            update_users_disk_quota(user, bytes_to_sum=gitlab_disk_usage)

            parameters = request.json
            publish_workflow_submission(workflow, user.id_, parameters)
        elif bundle_dir:
            # C1: seed the freshly created (empty) workspace from the validated
            # bundle, so the workspace -- not a separate client upload -- holds
            # the authoritative specification. The bundle equals the spec loaded
            # above, so no reload is needed here; the binding re-validation
            # happens at start.
            workflow = _get_workflow_with_uuid_or_name(
                response["workflow_id"], str(user.id_)
            )
            mv_workflow_files(bundle_dir, workflow.workspace_path)
            store_workflow_disk_quota(workflow, bytes_to_sum=bundle_bytes)
            update_users_disk_quota(user, bytes_to_sum=bundle_bytes)
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
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except REANAQuotaExceededError as e:
        if "git_url" in locals() and "git_commit_sha" in locals():
            _fail_gitlab_commit_build_status(user, git_url, git_commit_sha, str(e))
            return jsonify({"message": "Gitlab webhook was processed"}), 200
        return jsonify({"message": e.message}), 403
    except (KeyError, REANAValidationError) as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except RequestEntityTooLarge as e:
        return jsonify({"message": str(e)}), 413
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
    finally:
        if bundle_dir:
            shutil.rmtree(bundle_dir, ignore_errors=True)


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


def _start_workflow(workflow_id_or_name, user, **parameters):
    """Start given workflow by publishing it to the submission queue.

    This function is used by both the `set_workflow_status` and `start_workflow`.
    """
    operational_options = parameters.get("operational_options", {})
    input_parameters = parameters.get("input_parameters", {})
    restart = parameters.get("restart", False)
    reana_specification = parameters.get("reana_specification")

    try:
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        workflow = _get_workflow_with_uuid_or_name(workflow_id_or_name, str(user.id_))
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
            if reana_specification:
                restart_type = reana_specification.get("workflow", {}).get("type", None)
                if restart_type == "serial":
                    # Serial specifications are already self-contained and can
                    # be validated as the provided JSON payload. This preserves
                    # the direct API replacement flow even when the old shared
                    # restart workspace still has a reana.yaml.
                    validation_warnings = validate_loaded_spec(reana_specification)
                elif has_reana_spec_file(workflow.workspace_path):
                    # The Python client uploads a replacement reana.yaml into the
                    # shared restart workspace before calling /start. Reload that
                    # workspace now, before cloning the restart row, so non-serial
                    # replacement specs go through the sandboxed loader and the
                    # cloned row stores the authoritative serialized spec.
                    reana_specification, validation_warnings = load_and_validate_spec(
                        workflow.workspace_path
                    )
                else:
                    # Direct API users may still pass an already-serialized
                    # replacement spec. There is no raw bundle to load in this
                    # JSON-only endpoint, so validate the provided object as a
                    # serialized specification.
                    validation_warnings = validate_loaded_spec(reana_specification)
                restart_spec_validated = True
                workflow = clone_workflow(
                    workflow, reana_specification, restart_type, validate_spec=False
                )
            else:
                workflow = clone_workflow(workflow, None, None)
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
        #    replacement restarts were handled above before cloning, from either
        #    the uploaded workspace reana.yaml or a direct serialized payload; and
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
            workflow.reana_specification, validation_warnings = load_and_validate_spec(
                workflow.workspace_path
            )
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
        publish_workflow_submission(workflow, user.id_, submission_parameters)
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
        logging.error(traceback.format_exc())
        return e.response.json(), e.response.status_code
    except (REANAValidationError, ValidationError) as e:
        logging.error(traceback.format_exc())
        return {"message": str(e)}, 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return {"message": str(e)}, 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return {"message": str(e)}, 500


@blueprint.route("/workflows/<workflow_id_or_name>/start", methods=["POST"])
@signin_required()
@use_kwargs(
    {
        "operational_options": fields.Dict(),
        "input_parameters": fields.Dict(),
        "restart": fields.Boolean(),
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
              reana_specification:
                description: >-
                  Optional. Replace the original workflow specification with the given one.
                  Only considered when restarting a workflow.
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
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
              status:
                type: string
              user:
                type: string
              validation_warnings:
                description: >-
                  Non-blocking findings from re-validating the workspace
                  specification at start.
                type: array
                items:
                  type: object
          examples:
            application/json:
              {
                "message": "Workflow submitted",
                "id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
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
    response, status_code = _start_workflow(workflow_id_or_name, user, **parameters)
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
          description: Required. New workflow status.
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
              operational_options:
                description: >-
                  Optional. Additional operational options for workflow execution.
                  Only allowed when status is `start`.
                type: object
              input_parameters:
                description: >-
                  Optional. Additional input parameters that override the ones
                  from the workflow specification. Only allowed when status is `start`.
                type: object
              restart:
                description: >-
                  Optional. If true, the workflow is a restart of an earlier workflow execution.
                  Only allowed when status is `start`.
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
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
              status:
                type: string
              user:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow successfully launched",
                "id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
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
            # We can't call directly RWC when starting a workflow, as otherwise
            # the workflow would skip the queue. Instead, we do what the
            # `start_workflow` endpoint does.
            response, status_code = _start_workflow(
                workflow_id_or_name, user, **parameters
            )
            if "run_number" in response:
                # run_number is returned by `start_workflow`,
                # but not by `set_status_workflow`
                del response["run_number"]
            return jsonify(response), status_code

        parameters = request.json if request.is_json else None
        response, http_response = current_rwc_api_client.api.set_workflow_status(
            user=str(user.id_),
            workflow_id_or_name=workflow_id_or_name,
            status=status,
            parameters=parameters,
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


@blueprint.route("/workflows/<workflow_id_or_name>/workspace", methods=["POST"])
@signin_required()
@check_quota
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
        if not ("application/octet-stream" in request.headers.get("Content-Type")):
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

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        prevent_disk_quota_excess(
            user, request.content_length, action=f"Uploading file {filename}"
        )
        api_url = current_rwc_api_client.swagger_spec.__dict__.get("api_url")
        endpoint = current_rwc_api_client.api.upload_file.operation.path_name.format(
            workflow_id_or_name=workflow_id_or_name
        )
        http_response = requests.post(
            urlparse.urljoin(api_url, endpoint),
            data=RequestStreamWithLen(request.stream),
            params={"user": str(user.id_), "file_name": request.args.get("file_name")},
            headers={"Content-Type": "application/octet-stream"},
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
