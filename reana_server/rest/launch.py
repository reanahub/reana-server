# This file is part of REANA.
# Copyright (C) 2022, 2023, 2024, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server launch functionality Flask-Blueprint."""

import json
import logging
import os
import shutil
import traceback
import uuid

from bravado.exception import BravadoTimeoutError, HTTPError
from flask import Blueprint, jsonify
from jsonschema import ValidationError
import marshmallow
from marshmallow import Schema
from webargs import fields
from webargs.flaskparser import use_kwargs

from reana_commons.config import SHARED_VOLUME_PATH
from reana_commons.errors import REANAValidationError, REANAQuotaExceededError
from reana_commons.validation.utils import validate_workflow_name
from reana_db.database import Session
from reana_db.models import RunStatus
from reana_db.utils import (
    _get_workflow_with_uuid_or_name,
    build_workspace_path,
    store_workflow_disk_quota,
    update_users_disk_quota,
)

from reana_server.api_client import current_rwc_api_client
from reana_server.config import (
    FETCHER_ALLOWED_SCHEMES,
    RWC_MUTATION_CONNECT_TIMEOUT,
    RWC_MUTATION_READ_TIMEOUT,
)
from reana_server.decorators import check_quota, signin_required
from reana_server.fetcher import REANAFetcherError, get_fetcher
from reana_server.specification_bundles import (
    seed_workspace,
    stage_validation_snapshot,
    workspace_seed_members,
)
from reana_server.utils import (
    get_fetched_workflows_dir,
    prevent_disk_quota_excess,
    publish_workflow_submission,
    get_workspace_retention_rules,
)
from reana_server.validation import (
    SpecValidationServiceError,
    load_and_validate_spec,
    validate_input_parameters,
)
from reana_server.workspace_mutations import (
    WorkspaceMutationConflict,
    WorkspaceMutationUnavailable,
    workflow_creation_mutation_lock,
)
from reana_server.workflow_creation import create_workflow_on_controller

blueprint = Blueprint("launch", __name__)


def _resolve_created_workflow(response, expected_workflow_uuid, user_id):
    """Resolve the reserved workflow and remove any controller-created orphan."""
    returned_workflow_uuid = response.get("workflow_id")
    if returned_workflow_uuid == expected_workflow_uuid:
        return _get_workflow_with_uuid_or_name(expected_workflow_uuid, user_id)
    if returned_workflow_uuid:
        try:
            unexpected_workflow = _get_workflow_with_uuid_or_name(
                returned_workflow_uuid, user_id
            )
        except ValueError:
            pass
        else:
            unexpected_workflow.status = RunStatus.deleted
            Session.commit()
            shutil.rmtree(unexpected_workflow.workspace_path, ignore_errors=True)
    raise RuntimeError("Controller returned an unexpected workflow id.")


def _compensate_failed_creation(workflow):
    """Remove a controller-created workflow after an ambiguous failure."""
    workflow.status = RunStatus.deleted
    Session.commit()
    shutil.rmtree(workflow.workspace_path, ignore_errors=True)


@blueprint.route("/launch", methods=["POST"])
@use_kwargs(
    {
        "url": fields.Url(schemes=FETCHER_ALLOWED_SCHEMES, required=True),
        "name": fields.Str(),
        "parameters": fields.Str(),
        "specification": fields.Str(),
    },
    location="json",
    unknown=marshmallow.EXCLUDE,
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
        401:
          description: The request is not authenticated.
        503:
          description: >-
            The identity provider or the authentication session store is
            temporarily unavailable.
        403:
          description: >-
            Request failed. The user's compute quota has been exceeded.
          schema:
            type: object
            properties:
              message:
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
                "message": "Malformed request."
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
        409:
          description: Another workflow-family mutation is in progress.
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
    tmpdir = None
    validation_directory = None
    try:
        user_id = str(user.id_)
        tmpdir = get_fetched_workflows_dir(user_id)

        # Fetch the workflow spec
        fetcher = get_fetcher(url, tmpdir, specification)
        fetcher.fetch()
        specification_path = fetcher.workflow_spec_path()

        # Generate the workflow name
        workflow_name = name.replace(" ", "") or fetcher.generate_workflow_name()
        validate_workflow_name(workflow_name)

        # Load + validate the spec authoritatively. Loading runs in-process for
        # serial workflows and inside the sandboxed validator Job for
        # Snakemake/CWL/Yadage, so the API process never executes untrusted
        # workflow code. This sandboxing is what makes it safe to launch generic
        # Snakemake/CWL/Yadage workflows from any URL -- the untrusted spec
        # loading is confined to the disposable validator Job, not the API
        # server -- so no per-type URL allowlist is needed. Invalid specs are
        # rejected here (fail early).
        (
            validation_directory,
            _validation_relative_path,
            _validation_bytes,
            _legacy_parameters,
        ) = stage_validation_snapshot(specification_path, SHARED_VOLUME_PATH)
        reana_yaml, validation_warnings = load_and_validate_spec(validation_directory)
        input_parameters = json.loads(parameters)
        original_parameters = reana_yaml.get("inputs", {}).get("parameters", {})
        validate_input_parameters(input_parameters, original_parameters)

        # Build the same declared workspace seed a local client would produce:
        # workflow definition first, datasets/tests only after validation.
        seed_members, disk_usage = workspace_seed_members(specification_path)
        prevent_disk_quota_excess(
            user, disk_usage, action=f"Launching the workflow {workflow_name}"
        )

        # Get workspace retention rules
        retention_days = reana_yaml.get("workspace", {}).get("retention_days")
        retention_rules = get_workspace_retention_rules(retention_days)

        # Create workflow
        workflow_uuid = str(uuid.uuid4())
        workspace_path = build_workspace_path(user_id, workflow_uuid)
        workflow_dict = {
            "reana_specification": reana_yaml,
            "workflow_name": workflow_name,
            "workflow_id": workflow_uuid,
            "operational_options": {},
            "launcher_url": url,
            "retention_rules": retention_rules,
        }
        with workflow_creation_mutation_lock(user.id_, workflow_name, workspace_path):
            response, http_response = create_workflow_on_controller(
                lambda: current_rwc_api_client.api.create_workflow(
                    workflow=workflow_dict,
                    user=user_id,
                    _request_options={
                        "connect_timeout": RWC_MUTATION_CONNECT_TIMEOUT,
                        "timeout": RWC_MUTATION_READ_TIMEOUT,
                    },
                ).result(),
                workflow_uuid,
                user.id_,
                workspace_path,
                _compensate_failed_creation,
            )

            workflow = _resolve_created_workflow(response, workflow_uuid, user_id)
            if os.path.abspath(workflow.workspace_path) != os.path.abspath(
                workspace_path
            ):
                workflow.status = RunStatus.deleted
                Session.commit()
                shutil.rmtree(workflow.workspace_path, ignore_errors=True)
                raise RuntimeError("Controller created an unexpected workspace path.")
            try:
                copied_bytes = seed_workspace(seed_members, workflow.workspace_path)
                if copied_bytes != disk_usage:
                    raise REANAValidationError(
                        "The fetched workflow changed while its workspace was seeded."
                    )
            except Exception:
                workflow.status = RunStatus.deleted
                Session.commit()
                shutil.rmtree(workflow.workspace_path, ignore_errors=True)
                raise

            store_workflow_disk_quota(workflow, bytes_to_sum=disk_usage)
            update_users_disk_quota(user, bytes_to_sum=disk_usage)

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
    except BravadoTimeoutError:
        return jsonify({"message": "Workflow controller request timed out."}), 503
    except WorkspaceMutationConflict:
        return jsonify({"message": "The workflow family is currently changing."}), 409
    except WorkspaceMutationUnavailable:
        return jsonify({"message": "Workspace mutation serialization failed."}), 503
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
    except SpecValidationServiceError as e:
        # The validation service could not run (not an invalid specification):
        # surface it as a server-side error, not a "fetching" failure.
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
    except Exception:
        logging.error(traceback.format_exc())
        return (
            jsonify({"message": "Something went wrong while fetching the workflow."}),
            500,
        )
    finally:
        # Specification loading now happens in the sandboxed validator (or
        # in-process for serial, which does not change the cwd), so the previous
        # cwd/thread-safety limitation no longer applies and we can remove the
        # whole fetch directory.
        if validation_directory:
            shutil.rmtree(validation_directory, ignore_errors=True)
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


class LaunchSchema(Schema):
    """Marshmallow schema for ``launch`` endpoint."""

    workflow_id = fields.UUID()
    workflow_name = fields.Str()
    message = fields.Str()
    validation_warnings = fields.Raw()
