# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server Ping-functionality Flask-Blueprint."""

from flask import Blueprint, jsonify

blueprint = Blueprint('ping', __name__)


@blueprint.route('/ping', methods=['GET'])
def ping():  # noqa
    r"""Endpoint to ping the server. Responds with a pong.
    ---
    get:
      summary: Ping the server (healthcheck)
      operationId: ping
      description: >-
        Ping the server.
      produces:
       - application/json
      responses:
        200:
          description: >-
            Ping succeeded. Service is running and accessible.
          schema:
            type: object
            properties:
              message:
                type: string
              status:
                type: string
          examples:
            application/json:
              message: OK
              status: 200
    """

    return jsonify(message="OK", status="200"), 200
