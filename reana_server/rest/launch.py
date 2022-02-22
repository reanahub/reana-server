# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server launch functionality Flask-Blueprint."""

import json
import logging
import traceback

from flask import Blueprint, jsonify
from marshmallow import Schema
from webargs import fields
from webargs.flaskparser import use_kwargs


blueprint = Blueprint("launch", __name__)


@blueprint.route("/launch", methods=["POST"])
@use_kwargs(
    {
        "url": fields.Url(required=True),
        "name": fields.Str(),
        "parameters": fields.Str(),
    }
)
def launch(url, name, parameters="{}"):
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
        # FIXME: Information to retrieve once the workflow is submitted.
        mock_data = {
            "workflow_id": "cdcf48b1-c2f3-4693-8230-b066e088c6ac",
            "workflow_name": "mytest.1",
            "message": "The workflow has been successfully submitted.",
        }
        print(url, name, json.loads(parameters))
        return LaunchSchema().dump(mock_data)

    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


class LaunchSchema(Schema):
    """Marshmallow schema for ``launch`` endpoint."""

    workflow_id = fields.UUID()
    workflow_name = fields.Str()
    message = fields.Str()
