# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server status functionality Flask-Blueprint."""

import logging
import traceback

from flask import Blueprint, jsonify

from reana_server.decorators import signin_required
from reana_server.status import ClusterHealth, ClusterHealthSchema

blueprint = Blueprint("status", __name__)


@blueprint.route("/status")
@signin_required()
def status(**kwargs):  # noqa
    r"""Endpoint to retrieve Cluster health status.
    ---
    get:
      summary: Retrieve cluster health status
      operationId: status
      description: >-
        Retrieve cluster health status.
      produces:
       - application/json
      responses:
        200:
          description: >-
            Cluster health status information.
          schema:
            type: object
            properties:
              node:
                type: object
                properties:
                  available:
                    type: number
                  unschedulable:
                    type: number
                  percentage:
                    type: number
                  health:
                    type: string
                  sort:
                    type: number
              job:
                type: object
                properties:
                  running:
                    type: number
                  pending:
                    type: number
                  percentage:
                    type: number
                  health:
                    type: string
                  sort:
                    type: number
              workflow:
                type: object
                properties:
                  running:
                    type: number
                  queued:
                    type: number
                  pending:
                    type: number
                  percentage:
                    type: number
                  health:
                    type: string
                  sort:
                    type: number
              session:
                type: object
                properties:
                  active:
                    type: number
                  sort:
                    type: number
          examples:
            application/json:
              {
                "job": {
                    "pending": 2,
                    "running": 8,
                    "percentage": 20,
                    "health": "healthy",
                    "sort": 1
                },
                "node": {
                    "total": 4,
                    "unschedulable": 2,
                    "percentage": 33,
                    "health": "healthy",
                    "sort": 0
                },
                "session": {
                    "active": 3,
                    "sort": 3
                },
                "workflow": {
                    "queued": 2,
                    "running": 4,
                    "pending": 2,
                    "percentage": 50,
                    "health": "warning",
                    "sort": 2
                }
              }
    """
    try:
        cluster_health = ClusterHealth()
        return ClusterHealthSchema().dump(cluster_health)
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
