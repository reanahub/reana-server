# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021, 2022, 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server workflow-functionality Flask-Blueprint."""
import os
import json
import logging
import traceback

import requests
from bravado.exception import HTTPError
from flask import Blueprint, Response
from flask import jsonify, request, stream_with_context
from jsonschema.exceptions import ValidationError
from typing import Dict, List, Optional

from reana_commons import workspace
from reana_commons.config import (
    REANA_WORKFLOW_ENGINES,
    COMMAND_DANGEROUS_OPERATIONS,
    REANA_DEFAULT_SNAKEMAKE_ENV_IMAGE,
    WORKFLOW_RUNTIME_USER_GID,
    WORKFLOW_RUNTIME_USER_UID,
)
from reana_commons.errors import REANAQuotaExceededError, REANAValidationError
from reana_commons.utils import run_command
from reana_commons.specification import load_reana_spec, load_workflow_spec_from_reana_yaml, load_input_parameters
from reana_commons.validation.compute_backends import build_compute_backends_validator
from reana_commons.validation.operational_options import validate_operational_options
from reana_commons.validation.utils import validate_reana_yaml, validate_workflow_name, validate_workspace
from reana_commons.validation.parameters import build_parameters_validator
from reana_db.database import Session
from reana_db.models import InteractiveSessionType, RunStatus
from reana_db.utils import _get_workflow_with_uuid_or_name
from webargs import fields, validate
from webargs.flaskparser import use_kwargs

from reana_server.api_client import current_rwc_api_client
from reana_server.config import REANA_HOSTNAME
from reana_server.decorators import check_quota, signin_required
from reana_server.deleter import Deleter, InOrOut
from reana_server.gitlab_client import (
    GitLabClientRequestError,
    GitLabClientInvalidToken,
)
from reana_server.validation import (
    validate_inputs,
    validate_workspace_path,
    validate_workflow,
)
from reana_server.utils import (
    _fail_gitlab_commit_build_status,
    RequestStreamWithLen,
    _load_and_save_yadage_spec,
    _get_reana_yaml_from_gitlab,
    prevent_disk_quota_excess,
    publish_workflow_submission,
    clone_workflow,
    get_quota_excess_message,
    get_workspace_retention_rules,
    is_uuid_v4,
)

try:
    from urllib import parse as urlparse
except ImportError:
    from urlparse import urlparse

blueprint = Blueprint("workflows", __name__)


@blueprint.route("/workflows", methods=["GET"])
@use_kwargs(
    {
        "page": fields.Int(validate=validate.Range(min=1)),
        "size": fields.Int(validate=validate.Range(min=1)),
        "include_progress": fields.Bool(location="query"),
        "include_workspace_size": fields.Bool(location="query"),
        "workflow_id_or_name": fields.Str(),
    }
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
        This resource is expecting a REANA specification in JSON format with
        all the necessary information to instantiate a workflow.
      operationId: create_workflow
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_name
          in: query
          description: Name of the workflow to be created. If not provided
            name will be generated.
          required: true
          type: string
        # probably need to rename this to something more specific
        - name: spec
          in: query
          description: Remote repository which contains a valid REANA
            specification.
          required: false
          type: string
        - name: reana_specification
          in: body
          description: REANA specification with necessary data to instantiate
            a workflow.
          required: false
          schema:
            type: object
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
    try:
        if request.args.get("spec"):
            return jsonify("Not implemented"), 501

        if not request.is_json:
            raise Exception(
                "Either remote repository or REANA specification needs to be provided"
            )

        request_from_gitlab = "object_kind" in request.json
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
            git_data = {}
            reana_spec_file = request.json
            workflow_name = request.args.get("workflow_name", "")

        if user.has_exceeded_quota() and request_from_gitlab:
            message = f"User quota exceeded. Please check {REANA_HOSTNAME}"
            _fail_gitlab_commit_build_status(user, git_url, git_commit_sha, message)
            return jsonify({"message": "Gitlab webhook was processed"}), 200
        elif user.has_exceeded_quota():
            message = get_quota_excess_message(user)
            raise REANAQuotaExceededError(message)

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
        validate_workspace_path(reana_spec_file)

        validate_inputs(reana_spec_file)

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

        if git_data:
            workflow = _get_workflow_with_uuid_or_name(
                response["workflow_id"], str(user.id_)
            )

            # This is necessary for GitLab integration
            if workflow.type_ == "yadage":
                _load_and_save_yadage_spec(
                    workflow, workflow_dict["operational_options"]
                )
            elif workflow.type_ in ["cwl", "snakemake"]:
                reana_yaml_path = os.path.join(workflow.workspace_path, "reana.yaml")
                workflow.reana_specification = load_reana_spec(
                    reana_yaml_path, workflow.workspace_path
                )
                Session.commit()

            parameters = request.json
            publish_workflow_submission(workflow, user.id_, parameters)
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
        return jsonify({"message": e.message}), 403
    except (KeyError, REANAValidationError) as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


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
        workflow = _get_workflow_with_uuid_or_name(workflow_id_or_name, str(user.id_))

        return (
            jsonify(
                {
                    "specification": workflow.reana_specification,
                    "parameters": workflow.input_parameters,
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
    }
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
          examples:
            application/json:
              {
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
                "logs": "<Workflow engine log output>",
                "user": "00000000-0000-0000-0000-000000000000"
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


@blueprint.route("/workflows/<workflow_id_or_name>/start", methods=["POST"])
@signin_required()
@check_quota
def start_workflow(workflow_id_or_name, user):  # noqa
    r"""Start workflow.
    ---
    post:
      summary: Start workflow.
      description: >-
        This resource starts the workflow execution process.
        Resource is expecting a workflow UUID.
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
                type: object
              reana_specification:
                type: object
              input_parameters:
                type: object
              restart:
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
    try:
        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")
        parameters = request.json if request.is_json else {}
        workflow = _get_workflow_with_uuid_or_name(workflow_id_or_name, str(user.id_))
        operational_options = parameters.get("operational_options", {})
        operational_options = validate_operational_options(
            workflow.type_, operational_options
        )
        restart_type = None
        if "restart" in parameters:
            if workflow.status not in [RunStatus.finished, RunStatus.failed]:
                raise ValueError("Only finished or failed workflows can be restarted.")
            if workflow.workspace_has_pending_retention_rules():
                raise ValueError(
                    "The workflow cannot be restarted because some retention rules are "
                    "currently being applied to the workspace. Please retry later."
                )
            restart_type = (
                parameters.get("reana_specification", {})
                .get("workflow", {})
                .get("type", None)
            )
            workflow = clone_workflow(
                workflow, parameters.get("reana_specification", None), restart_type
            )
        elif workflow.status != RunStatus.created:
            raise ValueError(
                "Workflow {} is already {} and cannot be started "
                "again.".format(workflow.get_full_workflow_name(), workflow.status.name)
            )
        if "yadage" in (workflow.type_, restart_type):
            _load_and_save_yadage_spec(workflow, operational_options)

        input_parameters = parameters.get("input_parameters", {})
        validate_workflow(
            workflow.reana_specification, input_parameters=input_parameters
        )

        publish_workflow_submission(workflow, user.id_, parameters)
        response = {
            "message": "Workflow submitted.",
            "workflow_id": workflow.id_,
            "workflow_name": workflow.name,
            "status": RunStatus.queued.name,
            "run_number": workflow.run_number,
            "user": str(user.id_),
        }
        return jsonify(response), 200
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except (REANAValidationError, ValidationError) as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/workflows/<workflow_id_or_name>/status", methods=["PUT"])
@signin_required()
def set_workflow_status(workflow_id_or_name, user):  # noqa
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
              CACHE:
                type: string
              all_runs:
                type: boolean
              workspace:
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
        status = request.args.get("status")
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
    }
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
          description: Filter workflow workspace files.
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
            Request failed. The incoming payload seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Field 'size': Must be at least 1."
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
        workflow = _get_workflow_with_uuid_or_name(workflow_id_or_name, str(user.id_))
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
    }
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

@blueprint.route("/validation", methods=["POST"])
#@signin_required()
def workflow_validation():
    r"""Endpoint to validate reana yaml in the server. Responds with a result.
    ---
    post:
      summary: Validate reana yaml in the server
      operationId: validate_workflow
      description: >-
        Validate reana yaml in the server.
      consumes:
        - application/json
      produces:
       - application/json
      parameters:
        - name: reana_yaml
          in: body
          required: false
          description: >-
            The yaml file to create the workflow.
          schema:
            type: object
      responses:
        200:
          description: >-
            Get reana yaml validation results.
          schema:
            type: object
            properties:
              message:
                type: object
              status:
                type: string
          examples:
            application/json:
              message: OK
              status: 200
        400:
          description: >-
            Validation failed due to an error.
          schema:
            type: object
            properties:
              message:
                type: string
              status:
                type: string
          examples:
            application/json:
              message: Invalid REANA specification: None is not of type 'object'
              status: 400
    """
    reana_yaml = request.json
    logging.info("Received:")
    logging.info(reana_yaml)

    server_capabilities = []

    if "server_capabilities" in reana_yaml and (reana_yaml['server_capabilities'] != False):
      logging.info("Validating server capabilities")
      server_capabilities = validate_server_capabilities(reana_yaml)
      logging.info(server_capabilities)

    # delete server_capabilities section as it is no longer needed
    del reana_yaml['server_capabilities']


    runtime_params_warnings = []
    runtime_params_errors = []

    # If runtime parameters exist, get them
    if "runtime_parameters" in reana_yaml and (reana_yaml['runtime_parameters'] != False):
      runtime_parameters = reana_yaml['runtime_parameters']
      logging.info("runtime_parameters")
      logging.info(runtime_parameters)

      # Check for dangerous operations
      for parameter in runtime_parameters:
        for dangerous_command in COMMAND_DANGEROUS_OPERATIONS:
          if dangerous_command in runtime_parameters[parameter]:
            runtime_params_warnings.append('Operation "' + runtime_parameters[parameter] + '" might be dangerous.')

      # Check if a runtime parameter already exists in the provided reana_yaml
      parameters_in_yaml = []
      for parameter_name in reana_yaml["inputs"]["parameters"]:
          parameters_in_yaml.append(parameter_name)
      
      for parameter in runtime_parameters:
          if parameter not in parameters_in_yaml:
            runtime_params_errors.append('Command-line parameter "' + parameter + '" is not defined in reana.yaml.')
          
      logging.info("runtime_params_warnings")
      logging.info(runtime_params_warnings)

    # delete runtime parameters as they are no longer needed
    del reana_yaml['runtime_parameters']


    validate_environments = []

    if "skip_validate_environments" in reana_yaml and (reana_yaml['skip_validate_environments'] == False):
      logging.info("validate_environments_results")
      validate_environments_results = validate_environment(reana_yaml)
      logging.info(validate_environments_results)

    # delete skip_validate_environments section as it is no longer needed
    del reana_yaml['skip_validate_environments']


    try:
        reana_spec_file_warnings = validate_reana_yaml(reana_yaml)
    except Exception as e:
        return jsonify(message=str(e), status="400"), 400

    """Validate REANA specification file."""
    if "options" in reana_yaml.get("inputs", {}):
        workflow_type = reana_yaml["workflow"]["type"]
        workflow_options = reana_yaml["inputs"]["options"]
        try:
            validate_operational_options(workflow_type, workflow_options)
        except Exception as e:
            return jsonify(message=str(e), status="400"), 400

    """Validate parameters."""
    reana_spec_params_warnings = None
    try:
        reana_spec_params_warnings = validate_parameters(reana_yaml)
    except Exception as e:
        return jsonify(message=str(e), status="400"), 400

    response = {"reana_spec_file_warnings": reana_spec_file_warnings, 
                "reana_spec_params_warnings": json.dumps(vars(reana_spec_params_warnings), default=list),
                "runtime_params_warnings": runtime_params_warnings,
                "runtime_params_errors": runtime_params_errors,
                "server_capabilities": server_capabilities,
                "validate_environments": validate_environments}

    logging.info("Sending Response:")
    logging.info(response)

    return jsonify(message=response, status="200"), 200

def validate_parameters(reana_yaml: Dict) -> None:
    """Validate the presence of input parameters in workflow step commands and viceversa.

    :param reana_yaml: REANA YAML specification.
    """

    validator = build_parameters_validator(reana_yaml)
    try:
        validator.validate_parameters()
        return validator
    except REANAValidationError as e:
        raise e

def validate_server_capabilities(reana_yaml: Dict) -> None:
    """Validate server capabilities in REANA specification file.

    :param reana_yaml: dictionary which represents REANA specification file.
    """

    validation_results = []
    supported_backends = os.environ.get('REANA_COMPUTE_BACKENDS', None)
    validation_results.append(validate_compute_backends(reana_yaml, supported_backends))

    root_path = reana_yaml.get("workspace", {}).get("root_path")
    available_workspaces = os.environ.get('WORKSPACE_PATHS', None)
    validation_results.append(validate_available_workspace(root_path, available_workspaces))

    return validation_results

def validate_compute_backends(
    reana_yaml: Dict, supported_backends: Optional[List[str]]
) -> None:
    """Validate compute backends in REANA specification file according to workflow type.

    :param reana_yaml: dictionary which represents REANA specification file.
    :param supported_backends: a list of the supported compute backends.
    """

    validator = build_compute_backends_validator(reana_yaml, supported_backends)
    try:
        validator.validate()
    except REANAValidationError as e:
        return {"message": str(e), "msg_type": "error"}

    return {"message": "Workflow compute backends appear to be valid.", "msg_type": "success"}

def validate_available_workspace(
    root_path: str, available_workspaces: Optional[List[str]]
) -> None:
    """Validate workspace in REANA specification file.

    :param root_path: workspace root path to be validated.
    :param available_workspaces: a list of the available workspaces.

    :raises ValidationError: Given workspace in REANA spec file does not validate against
        allowed workspaces.
    """
    if root_path:
        try:
            validate_workspace(root_path, available_workspaces)
            return {"message": "Workflow workspace appears valid.", "msg_type": "success"}
        except REANAValidationError as e:
            return {"message": str(e), "msg_type": "error"}

# Environment validation parts 

def validate_environment(reana_yaml, pull=False):
    """Validate environments in REANA specification file according to workflow type.

    :param reana_yaml: Dictionary which represents REANA specification file.
    :param pull: If true, attempt to pull remote environment image to perform GID/UID validation.
    """

    def build_validator(workflow):
        workflow_type = workflow["type"]
        if workflow_type == "serial":
            workflow_steps = workflow["specification"]["steps"]
            return EnvironmentValidatorSerial(workflow_steps=workflow_steps, pull=pull)
        if workflow_type == "yadage":
            workflow_steps = workflow["specification"]["stages"]
            return EnvironmentValidatorYadage(workflow_steps=workflow_steps, pull=pull)
        if workflow_type == "cwl":
            workflow_steps = workflow.get("specification", {}).get("$graph", workflow)
            return EnvironmentValidatorCWL(workflow_steps=workflow_steps, pull=pull)
        if workflow_type == "snakemake":
            workflow_steps = workflow["specification"]["steps"]
            return EnvironmentValidatorSnakemake(
                workflow_steps=workflow_steps, pull=pull
            )

    workflow = reana_yaml["workflow"]
    validator = build_validator(workflow)
    messages = validator.validate()
    return messages


DOCKER_REGISTRY_INDEX_URL = (
    "https://hub.docker.com/v2/repositories/{repository}{image}/tags/{tag}"
)
"""Docker Hub registry index URL."""

DOCKER_REGISTRY_PREFIX = "docker.io"
"""Prefix for DockerHub image registry."""

ENVIRONMENT_IMAGE_SUSPECTED_TAGS_VALIDATOR = ["latest", "master", ""]
"""Warns user if above environment image tags are used."""

GITLAB_CERN_REGISTRY_INDEX_URL = (
    "https://gitlab.cern.ch/api/v4/projects/{image}/registry/repositories?tags=1"
)
"""GitLab CERN registry index URL."""

GITLAB_CERN_REGISTRY_PREFIX = "gitlab-registry.cern.ch"
"""Prefix for GitLab image registry at CERN."""

class EnvironmentValidationError(Exception):
    """REANA workflow environment validation didn't succeed."""

class EnvironmentValidatorBase:
    """REANA workflow environments validation base class."""

    def __init__(self, workflow_steps=None, pull=False):
        """Validate environments in REANA workflow.

        :param workflow_steps: List of dictionaries which represents different steps involved in workflow.
        :param pull: If true, attempt to pull remote environment image to perform GID/UID validation.
        """
        self.workflow_steps = workflow_steps
        self.pull = pull
        self.validated_images = set()
        self.messages = []

    def validate(self):
        """Validate REANA workflow environments."""
        try:
            self.validate_environment()
        except EnvironmentValidationError as e:
            self.messages.append({"type": "error", "message": str(e)})

        return self.messages

    def validate_environment(self):
        """Validate environments in REANA workflow."""
        raise NotImplementedError

    def _validate_environment_image(self, image, kubernetes_uid=None):
        """Validate image environment.

        :param image: Full image name with tag if specified.
        :param kubernetes_uid: Kubernetes UID defined in workflow spec.
        """

        if image not in self.validated_images:
            image_name, image_tag = self._validate_image_tag(image)
            exists_locally, _ = self._image_exists(image_name, image_tag)
            if exists_locally or self.pull:
                uid, gids = self._get_image_uid_gids(image_name, image_tag)
                self._validate_uid_gids(uid, gids, kubernetes_uid=kubernetes_uid)
            else:
                self.messages.append(
                    {
                        "type": "warning",
                        "message": "UID/GIDs validation skipped, specify `--pull` to enable it.",
                    }
                )
            self.validated_images.add(image)

    def _image_exists(self, image, tag):
        """Verify if image exists locally or remotely.

        :returns: A tuple with two boolean values: image exists locally, image exists remotely.
        """

        image_exists_remotely = (
            self._image_exists_in_gitlab_cern
            if image.startswith(GITLAB_CERN_REGISTRY_PREFIX)
            else self._image_exists_in_dockerhub
        )

        exists_locally = self._image_exists_locally(image, tag)
        exists_remotely = image_exists_remotely(image, tag)

        if not any([exists_locally, exists_remotely]):
            raise EnvironmentValidationError(
                "Environment image {} does not exist locally or remotely.".format(
                    self._get_full_image_name(image, tag)
                )
            )
        return exists_locally, exists_remotely

    def _validate_uid_gids(self, uid, gids, kubernetes_uid=None):
        """Check whether container UID and GIDs are valid."""
        if WORKFLOW_RUNTIME_USER_GID not in gids:
            if kubernetes_uid is None:
                raise EnvironmentValidationError(
                    "Environment image GID must be {}. GIDs {} were found.".format(
                        WORKFLOW_RUNTIME_USER_GID, gids
                    )
                )
            else:
                self.messages.append(
                    {
                        "type": "warning",
                        "message": "Environment image GID is recommended to be {}. GIDs {} were found.".format(
                            WORKFLOW_RUNTIME_USER_GID, gids
                        ),
                    }
                )
        if kubernetes_uid is not None:
            if kubernetes_uid != uid:
                self.messages.append(
                    {
                        "type": "warning",
                        "message": "`kubernetes_uid` set to {}. UID {} was found.".format(
                            kubernetes_uid, uid
                        ),
                    }
                )
        elif uid != WORKFLOW_RUNTIME_USER_UID:
            self.messages.append(
                {
                    "type": "info",
                    "message": "Environment image uses UID {} but will run as UID {}.".format(
                        uid, WORKFLOW_RUNTIME_USER_UID
                    ),
                }
            )

    def _validate_image_tag(self, image):
        """Validate if image tag is valid."""
        image_name, image_tag = "", ""
        message = {
            "type": "success",
            "message": "Environment image {} has the correct format.".format(image),
        }
        if " " in image:
            raise EnvironmentValidationError(
                f"Environment image '{image}' contains illegal characters."
            )
        if ":" in image:
            environment = image.split(":", 1)
            image_name, image_tag = environment[0], environment[-1]
            if ":" in image_tag:
                raise EnvironmentValidationError(
                    "Environment image {} has invalid tag '{}'".format(
                        image_name, image_tag
                    )
                )
            elif image_tag in ENVIRONMENT_IMAGE_SUSPECTED_TAGS_VALIDATOR:
                message = {
                    "type": "warning",
                    "message": "Using '{}' tag is not recommended in {} environment image.".format(
                        image_tag, image_name
                    ),
                }
        else:
            message = {
                "type": "warning",
                "message": "Environment image {} does not have an explicit tag.".format(
                    image
                ),
            }
            image_name = image

        self.messages.append(message)
        return image_name, image_tag

    def _image_exists_locally(self, image, tag):
        """Verify if image exists locally."""
        full_image = self._get_full_image_name(image, tag or "latest")
        #image_id = run_command(
        #    f'docker images -q "{full_image}"', display=False, return_output=True
        #)
        image_id = False
        # TODO: replace above line with the commented out lines above when sandbox gets implemented
        if image_id:
            self.messages.append(
                {
                    "type": "success",
                    "message": f"Environment image {full_image} exists locally.",
                }
            )
            return True
        else:
            self.messages.append(
                {
                    "type": "warning",
                    "message": f"Environment image {full_image} does not exist locally.",
                }
            )
            return False

    def _image_exists_in_gitlab_cern(self, image, tag):
        """Verify if image exists in GitLab CERN."""
        # Remove registry prefix
        prefixed_image = image
        full_prefixed_image = self._get_full_image_name(image, tag or "latest")
        image = image.split("/", 1)[-1]
        # Encode image name slashes
        remote_registry_url = GITLAB_CERN_REGISTRY_INDEX_URL.format(
            image=requests.utils.quote(image, safe="")
        )
        try:
            # FIXME: if image is private we can't access it, we'd
            # need to pass a GitLab API token generated from the UI.
            response = requests.get(remote_registry_url)
        except requests.exceptions.RequestException as e:
            logging.error(e)
            self.messages.append(
                {
                    "type": "error",
                    "message": "Something went wrong when querying {}".format(
                        remote_registry_url
                    ),
                }
            )
            return False

        if not response.ok:
            msg = response.json().get("message")
            self.messages.append(
                {
                    "type": "warning",
                    "message": "Existence of environment image {} in GitLab CERN could not be verified: {}".format(
                        self._get_full_image_name(prefixed_image, tag), msg
                    ),
                }
            )
            return False
        else:
            # If not tag was set, use `latest` (default) to verify.
            tag = tag or "latest"
            tag_exists = any(
                tag_dict["name"] == tag for tag_dict in response.json()[0].get("tags")
            )
            if tag_exists:
                self.messages.append(
                    {
                        "type": "success",
                        "message": "Environment image {} exists in GitLab CERN.".format(
                            full_prefixed_image
                        ),
                    }
                )
                return True
            else:
                self.messages.append(
                    {
                        "type": "warning",
                        "message": 'Environment image {} in GitLab CERN does not exist: Tag "{}" missing.'.format(
                            full_prefixed_image, tag
                        ),
                    }
                )
                return False

    def _image_exists_in_dockerhub(self, image, tag):
        """Verify if image exists in DockerHub."""
        full_image = self._get_full_image_name(image, tag or "latest")
        # remove leading `docker.io/` prefix, if present
        dockerhub_prefix = f"{DOCKER_REGISTRY_PREFIX}/"
        if image.startswith(dockerhub_prefix):
            image = image[len(dockerhub_prefix) :]
        # Some images like `python:2.7-slim` require to specify `library`
        # as a repository in order to work with DockerHub API v2
        repository = "" if "/" in image else "library/"
        docker_registry_url = DOCKER_REGISTRY_INDEX_URL.format(
            repository=repository, image=image, tag=tag
        )
        # Remove traling slash if no tag was specified
        if not tag:
            docker_registry_url = docker_registry_url[:-1]
        try:
            response = requests.get(docker_registry_url)
        except requests.exceptions.RequestException as e:
            logging.error(e)
            self.messages.append(
                {
                    "type": "error",
                    "message": "Something went wrong when querying {}".format(
                        docker_registry_url
                    ),
                }
            )
            return False

        if not response.ok:
            if response.status_code == 404:
                msg = response.json().get("message")
                self.messages.append(
                    {
                        "type": "warning",
                        "message": "Environment image {} does not exist in Docker Hub: {}".format(
                            full_image, msg
                        ),
                    }
                )
                return False
            else:
                self.messages.append(
                    {
                        "type": "warning",
                        "message": "==> WARNING: Existence of environment image {} in Docker Hub could not be verified. Status code: {} {}".format(
                            full_image,
                            response.status_code,
                            response.reason,
                        ),
                    }
                )
                return False
        else:
            self.messages.append(
                {
                    "type": "success",
                    "message": "Environment image {} exists in Docker Hub.".format(
                        full_image
                    ),
                }
            )
            return True

    def _get_image_uid_gids(self, image, tag):
        """Obtain environment image UID and GIDs.

        :returns: A tuple with UID and GIDs.
        """
        return 1000, 0
        # TODO: remove the above line when sandbox gets implemented

        from reana_commons.utils import run_command

        # Check if docker is installed.
        run_command("docker version", display=False, return_output=True)
        # Run ``id``` command inside the container.
        uid_gid_output = run_command(
            f'docker run -i -t --rm --entrypoint /bin/sh {self._get_full_image_name(image, tag)} -c "/usr/bin/id -u && /usr/bin/id -G"',
            display=False,
            return_output=True,
        )
        ids = uid_gid_output.splitlines()
        uid, gids = (
            int(ids[-2]),
            [int(gid) for gid in ids[-1].split()],
        )
        return uid, gids

    def _get_full_image_name(self, image, tag=None):
        """Return full image name with tag if is passed."""
        return "{}{}".format(image, ":{}".format(tag) if tag else "")


class EnvironmentValidatorSerial(EnvironmentValidatorBase):
    """REANA serial workflow environments validation."""

    def validate_environment(self):
        """Validate environments in REANA serial workflow."""
        for step in self.workflow_steps:
            image = step["environment"]
            kubernetes_uid = step.get("kubernetes_uid")
            self._validate_environment_image(image, kubernetes_uid=kubernetes_uid)


class EnvironmentValidatorYadage(EnvironmentValidatorBase):
    """REANA yadage workflow environments validation."""

    def _extract_steps_environments(self):
        """Extract environments yadage workflow steps."""

        def traverse_yadage_workflow(stages):
            environments = []
            for stage in stages:
                if "workflow" in stage["scheduler"]:
                    nested_stages = stage["scheduler"]["workflow"].get("stages", {})
                    environments += traverse_yadage_workflow(nested_stages)
                else:
                    environments.append(stage["scheduler"]["step"]["environment"])
            return environments

        return traverse_yadage_workflow(self.workflow_steps)

    def validate_environment(self):
        """Validate environments in REANA yadage workflow."""

        def _check_environment(environment):
            image = "{}{}".format(
                environment["image"],
                (
                    ":{}".format(environment["imagetag"])
                    if "imagetag" in environment
                    else ""
                ),
            )
            k8s_uid = next(
                (
                    resource["kubernetes_uid"]
                    for resource in environment.get("resources", [])
                    if "kubernetes_uid" in resource
                ),
                None,
            )
            self._validate_environment_image(image, kubernetes_uid=k8s_uid)

        steps_environments = self._extract_steps_environments()
        for environment in steps_environments:
            if environment["environment_type"] != "docker-encapsulated":
                raise EnvironmentValidationError(
                    'The only Yadage environment type supported is "docker-encapsulated". Found "{}".'.format(
                        environment["environment_type"]
                    )
                )
            else:
                _check_environment(environment)


class EnvironmentValidatorCWL(EnvironmentValidatorBase):
    """REANA CWL workflow environments validation."""

    def validate_environment(self):
        """Validate environments in REANA CWL workflow."""

        def _validate_workflow_environment(workflow_steps):
            """Validate environments in REANA CWL workflow steps."""
            requirements = workflow_steps.get("requirements", [])
            images = list(filter(lambda req: "dockerPull" in req, requirements))

            for image in images:
                self._validate_environment_image(image["dockerPull"])

        workflow = self.workflow_steps
        if isinstance(workflow, dict):
            _validate_workflow_environment(workflow)
        elif isinstance(workflow, list):
            for wf in workflow:
                _validate_workflow_environment(wf)


class EnvironmentValidatorSnakemake(EnvironmentValidatorBase):
    """REANA Snakemake workflow environments validation."""

    def validate_environment(self):
        """Validate environments in REANA Snakemake workflow."""
        for step in self.workflow_steps:
            image = step["environment"]
            if not image:
                self.messages.append(
                    {
                        "type": "warning",
                        "message": f"Environment image not specified, using {REANA_DEFAULT_SNAKEMAKE_ENV_IMAGE}.",
                    }
                )
                image = REANA_DEFAULT_SNAKEMAKE_ENV_IMAGE
            kubernetes_uid = step.get("kubernetes_uid")
            self._validate_environment_image(image, kubernetes_uid=kubernetes_uid)
  