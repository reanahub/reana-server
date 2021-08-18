# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server Workspaces functionality Flask-Blueprint."""

import logging
import traceback
from flask import Blueprint, jsonify

from reana_commons.config import (
    WORKSPACE_PATHS,
    DEFAULT_WORKSPACE_PATH,
)

blueprint = Blueprint("workspaces", __name__)


@blueprint.route("/workspaces", methods=["GET"])
def workspaces():  # noqa
    r"""Get the list of available workspaces.

    ---
    get:
      summary: Get the list of available workspaces.
      operationId: workspaces
      description: >-
        This resource reports the available workspaces in the cluster.
      produces:
       - application/json
      responses:
        200:
          description: >-
            Request succeeded. The response contains the list of all workspaces.
          schema:
            type: object
            properties:
              workspaces_available:
                type: array
                items:
                  type: string
          examples:
            application/json:
              {
                "workspaces_available": ["/usr/share","/eos/home","/var/reana"],
                "default": "/usr/share"
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        response = {
            "workspaces_available": WORKSPACE_PATHS,
            "default": DEFAULT_WORKSPACE_PATH,
        }
        return jsonify(response), 200

    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
