# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2021, 2022 CERN.
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
@signin_required(token_required=False)
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
                  total:
                    type: number
              job:
                type: object
                properties:
                  available:
                    type: number
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
                  total:
                    type: number
              workflow:
                type: object
                properties:
                  available:
                    type: number
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
                  total:
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
                    "total": 8,
                    "pending": 3,
                    "running": 2,
                    "available": 3,
                    "percentage": 38,
                    "health": "warning",
                    "sort": 1
                },
                "node": {
                    "total": 10,
                    "available": 8,
                    "unschedulable": 2,
                    "percentage": 80,
                    "health": "healthy",
                    "sort": 0
                },
                "session": {
                    "active": 3,
                    "sort": 3
                },
                "workflow": {
                    "total": 30,
                    "available": 24,
                    "queued": 2,
                    "running": 4,
                    "pending": 2,
                    "percentage": 80,
                    "health": "healthy",
                    "sort": 2
                }
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
        cluster_health = ClusterHealth()
        return ClusterHealthSchema().dump(cluster_health)
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
