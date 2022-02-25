# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server launch functionality Flask-Blueprint."""

import json
import logging
import traceback

from bravado.exception import HTTPError
from flask import Blueprint, jsonify
from jsonschema import ValidationError
from marshmallow import Schema
from webargs import fields
from webargs.flaskparser import use_kwargs

from reana_commons.errors import REANAValidationError
from reana_commons.specification import load_reana_spec
from reana_commons.validation.utils import validate_workflow_name
from reana_db.utils import _get_workflow_with_uuid_or_name

from reana_server.api_client import current_rwc_api_client
from reana_server.decorators import signin_required
from reana_server.fetcher import get_fetcher
from reana_server.utils import (
    get_fetched_workflows_dir,
    mv_workflow_files,
    publish_workflow_submission,
    remove_fetched_workflows_dir,
)
from reana_server.validation import validate_workflow


blueprint = Blueprint("launch", __name__)


@blueprint.route("/launch", methods=["POST"])
@use_kwargs(
    {
        "url": fields.Url(required=True),
        "name": fields.Str(),
        "parameters": fields.Str(),
    }
)
@signin_required()
def launch(user, url, name="", parameters="{}"):
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
      responses:
        200:
          description: >-
            Request succeeded. Information of the workflow launched.
          schema:
            type: object
            properties:
              workflow_id:
                type: string
              workflow_name:
                type: string
              message:
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
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        500:
          description: >-
            Request failed. Internal server error.
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """
    try:
        user_id = str(user.id_)
        tmpdir = get_fetched_workflows_dir(user_id)

        # Fetch and load workflow spec
        fetcher = get_fetcher(url, tmpdir)
        fetcher.fetch()
        spec_path = fetcher.workflow_spec_path()
        reana_yaml = load_reana_spec(spec_path)

        # Validate workflow spec
        input_parameters = json.loads(parameters)
        validate_workflow(reana_yaml, input_parameters)

        workflow_name = name.replace(" ", "") or "workflow"
        validate_workflow_name(workflow_name)

        # Create workflow
        workflow_dict = {
            "reana_specification": reana_yaml,
            "workflow_name": workflow_name,
            "operational_options": {},
            "launcher_url": url,
        }
        response, http_response = current_rwc_api_client.api.create_workflow(
            workflow=workflow_dict, user=user_id,
        ).result()

        workflow = _get_workflow_with_uuid_or_name(response["workflow_id"], user_id)
        mv_workflow_files(tmpdir, workflow.workspace_path)

        # Start the workflow
        parameters = {"input_parameters": input_parameters}
        publish_workflow_submission(workflow, user.id_, parameters)
        response_data = {
            "workflow_id": workflow.id_,
            "workflow_name": workflow.name,
            "message": "The workflow has been successfully submitted.",
        }
        return LaunchSchema().dump(response_data)
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except (REANAValidationError, ValueError, ValidationError) as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
    finally:
        remove_fetched_workflows_dir(tmpdir)


class LaunchSchema(Schema):
    """Marshmallow schema for ``launch`` endpoint."""

    workflow_id = fields.UUID()
    workflow_name = fields.Str()
    message = fields.Str()
