# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2021, 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server info functionality Flask-Blueprint."""

import logging
import traceback

from flask import Blueprint, jsonify
from marshmallow import Schema, fields

from reana_commons.config import DEFAULT_WORKSPACE_PATH, WORKSPACE_PATHS

from reana_server.config import SUPPORTED_COMPUTE_BACKENDS
from reana_server.decorators import signin_required

blueprint = Blueprint("info", __name__)


@blueprint.route("/info", methods=["GET"])
@signin_required()
def info(user, **kwargs):  # noqa
    r"""Get information about the cluster capabilities.

    ---
    get:
      summary: Get information about the cluster capabilities.
      operationId: info
      description: >-
        This resource reports information about cluster capabilities.
      produces:
       - application/json
      parameters:
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. The response contains general info about the cluster.
          schema:
            type: object
            properties:
              workspaces_available:
                type: object
                properties:
                  title:
                    type: string
                  value:
                    type: array
                    items:
                      type: string
              default_workspace:
                type: object
                properties:
                  title:
                    type: string
                  value:
                    type: string
              compute_backends:
                type: object
                properties:
                  title:
                    type: string
                  value:
                    type: array
                    items:
                      type: string
          examples:
            application/json:
              {
                "workspaces_available": {
                    "title": "List of available workspaces",
                    "value": ["/usr/share","/eos/home","/var/reana"]
                },
                "default_workspace": {
                    "title": "Default workspace",
                    "value": "/usr/share"
                },
                "compute_backends": {
                    "title": "List of supported compute backends",
                    "value": [
                        "kubernetes",
                        "htcondorcern",
                        "slurmcern"
                    ]
              }
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        info = dict(
            workspaces_available=dict(
                title="List of available workspaces",
                value=list(WORKSPACE_PATHS.values()),
            ),
            default_workspace=dict(
                title="Default workspace", value=DEFAULT_WORKSPACE_PATH
            ),
            compute_backends=dict(
                title="List of supported compute backends",
                value=SUPPORTED_COMPUTE_BACKENDS,
            ),
        )
        return InfoSchema().dump(info)

    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


class InfoSchema(Schema):
    """Marshmallow schema for ``info`` endpoint."""

    workspaces_available = fields.Dict(
        keys=fields.Str(), values=fields.List(fields.Str())
    )
    default_workspace = fields.Dict(keys=fields.Str(), values=fields.Str())
    compute_backends = fields.Dict(keys=fields.Str(), values=fields.List(fields.Str()))
