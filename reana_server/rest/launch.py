# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server launch functionality Flask-Blueprint."""

import json
import logging
import os
import shutil
import threading
import traceback

from bravado.exception import HTTPError
from flask import Blueprint, jsonify
from jsonschema import ValidationError
from marshmallow import Schema
from webargs import fields
from webargs.flaskparser import use_kwargs
import yaml

from reana_commons.errors import REANAValidationError, REANAQuotaExceededError
from reana_commons.specification import load_reana_spec
from reana_commons.validation.utils import validate_workflow_name
from reana_db.utils import (
    _get_workflow_with_uuid_or_name,
    get_disk_usage_or_zero,
    store_workflow_disk_quota,
    update_users_disk_quota,
)

from reana_server.api_client import current_rwc_api_client
from reana_server.config import FETCHER_ALLOWED_SCHEMES, LAUNCHER_ALLOWED_SNAKEMAKE_URLS
from reana_server.decorators import check_quota, signin_required
from reana_server.fetcher import REANAFetcherError, get_fetcher
from reana_server.utils import (
    get_fetched_workflows_dir,
    mv_workflow_files,
    prevent_disk_quota_excess,
    publish_workflow_submission,
    filter_input_files,
    get_workspace_retention_rules,
)
from reana_server.validation import validate_workflow


blueprint = Blueprint("launch", __name__)

load_reana_spec_lock = threading.Lock()
"""Lock used to make sure only one specification is loaded at a time."""


@blueprint.route("/launch", methods=["POST"])
@use_kwargs(
    {
        "url": fields.Url(schemes=FETCHER_ALLOWED_SCHEMES, required=True),
        "name": fields.Str(),
        "parameters": fields.Str(),
        "specification": fields.Str(),
    }
)
@signin_required()
@check_quota
def launch(user, url, name="", parameters="{}", specification=None):
    r"""Endpoint to launch a REANA workflow from URL.

    ---
    post:
      summary: Launch workflow from a remote REANA specification file.
      description: >-
        This resource expects a remote reference to a REANA specification
        file needed to launch a workflow via URL.
      operationId: launch
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: data
          in: body
          description: The remote origin data required to launch a workflow.
          schema:
            type: object
            required:
              - url
            properties:
              url:
                description: Remote origin URL where the REANA specification file is hosted.
                type: string
              name:
                description: Workflow name.
                type: string
              parameters:
                description: Workflow parameters.
                type: string
              specification:
                description: Path to the workflow specification file to be used.
                type: string
      responses:
        200:
          description: >-
            Request succeeded. Information of the workflow launched.
          schema:
            type: object
            required:
              - workflow_id
              - workflow_name
              - message
            properties:
              workflow_id:
                type: string
              workflow_name:
                type: string
              message:
                type: string
              validation_warnings:
                description: >-
                    Dictionary of validation warnings, if any. Each
                    key is a property that was not correctly validated.
                type: object
                properties:
                  additional_properties:
                    type: array
                    items:
                      type: string
          examples:
            application/json:
              {
                "workflow_id": "cdcf48b1-c2f3-4693-8230-b066e088c6ac",
                "workflow_name": "mytest.1",
                "message": "The workflow has been successfully submitted."
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
        user_id = str(user.id_)
        tmpdir = get_fetched_workflows_dir(user_id)

        # Fetch the workflow spec
        fetcher = get_fetcher(url, tmpdir, specification)
        fetcher.fetch()

        # Generate the workflow name
        workflow_name = name.replace(" ", "") or fetcher.generate_workflow_name()
        validate_workflow_name(workflow_name)

        # Load and validate the workflow spec
        spec_path = fetcher.workflow_spec_path()

        # When launching a snakemake workflow, check if the url is allowed
        # FIXME: This will not be needed when using a sandbox
        with open(spec_path) as spec_fd:
            reana_yaml = yaml.safe_load(spec_fd.read())
            workflow_type = reana_yaml["workflow"]["type"]
            if (
                workflow_type == "snakemake"
                and url not in LAUNCHER_ALLOWED_SNAKEMAKE_URLS
            ):
                raise ValidationError(
                    "Unfortunately, it is not possible to launch generic Snakemake "
                    "workflows at the moment. Please contact the REANA admins for "
                    "more information."
                )

        # FIXME: locking will not be needed when the loading and validation of
        # specifications will be done inside an external sandbox
        with load_reana_spec_lock:
            reana_yaml = load_reana_spec(spec_path, workspace_path=tmpdir)
        input_parameters = json.loads(parameters)
        validation_warnings = validate_workflow(reana_yaml, input_parameters)

        # Keep only files and directories listed as workflow's inputs
        filter_input_files(tmpdir, reana_yaml)

        # Check the user's disk quota
        disk_usage = get_disk_usage_or_zero(tmpdir)
        prevent_disk_quota_excess(
            user, disk_usage, action=f"Launching the workflow {workflow_name}"
        )

        # Get workspace retention rules
        retention_days = reana_yaml.get("workspace", {}).get("retention_days")
        retention_rules = get_workspace_retention_rules(retention_days)

        # Create workflow
        workflow_dict = {
            "reana_specification": reana_yaml,
            "workflow_name": workflow_name,
            "operational_options": {},
            "launcher_url": url,
            "retention_rules": retention_rules,
        }
        response, http_response = current_rwc_api_client.api.create_workflow(
            workflow=workflow_dict,
            user=user_id,
        ).result()

        workflow = _get_workflow_with_uuid_or_name(response["workflow_id"], user_id)
        mv_workflow_files(tmpdir, workflow.workspace_path)

        # Update the workflows's and user's disk usage
        store_workflow_disk_quota(workflow, bytes_to_sum=disk_usage)
        update_users_disk_quota(user, bytes_to_sum=disk_usage)

        # Start the workflow
        parameters = {"input_parameters": input_parameters}
        publish_workflow_submission(workflow, user.id_, parameters)
        response_data = {
            "workflow_id": workflow.id_,
            "workflow_name": workflow.name,
            "message": "The workflow has been successfully submitted.",
        }
        if validation_warnings:
            response_data["message"] = (
                "The workflow has been successfully submitted, but some warnings were issued."
            )
            response_data["validation_warnings"] = validation_warnings
        return LaunchSchema().dump(response_data)
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except json.JSONDecodeError:
        logging.error(traceback.format_exc())
        return (
            jsonify({"message": "The workflow 'parameters' field is not valid JSON."}),
            400,
        )
    except REANAQuotaExceededError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except (
        REANAFetcherError,
        REANAValidationError,
        ValueError,
        ValidationError,
    ) as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception:
        logging.error(traceback.format_exc())
        return (
            jsonify({"message": "Something went wrong while fetching the workflow."}),
            500,
        )
    finally:
        # FIXME: `load_reana_spec` is not thread-safe and it changes the cwd, so for the
        # time being we can only delete the files inside the directory where the
        # workflow is fetched.  Deleting the directory would result in a
        # `FileNotFoundError` when calling `os.getcwd()`, due to the cwd not existing
        # anymore.
        #
        # remove_fetched_workflows_dir(tmpdir)
        for entry in os.scandir(tmpdir):
            if entry.is_file() or entry.is_symlink():
                os.remove(entry)
            else:
                shutil.rmtree(entry)


class LaunchSchema(Schema):
    """Marshmallow schema for ``launch`` endpoint."""

    workflow_id = fields.UUID()
    workflow_name = fields.Str()
    message = fields.Str()
    validation_warnings = fields.Dict()
