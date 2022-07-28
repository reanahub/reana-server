# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020, 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server config-functionality Flask-Blueprint."""

import logging
import traceback

from flask import Blueprint, jsonify
from reana_commons.config import REANAConfig


blueprint = Blueprint("config", __name__)


@blueprint.route("/config", methods=["GET"])
def get_config():
    r"""Endpoint to get Reana-UI configuration.

    ---
    get:
      summary: Gets information about Reana-UI configuration user.
      description: >-
        This resource provides configuration needed by Reana-UI.
      operationId: get_config
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: API access_token of user.
          required: false
          type: string
      responses:
        200:
          description: >-
            Configuration information to consume by Reana-UI.
          schema:
            type: object
          examples:
            application/json:
              {
                "announcement": "This is a QA instance",
                "chat_url": "https://mattermost.web.cern.ch/it-dep/channels/reana",
                "client_pyvenv": "/afs/cern.ch/user/r/reana/public/reana/bin/activate",
                "docs_url": "http://docs.reana.io/",
                "forum_url": "https://forum.reana.io/",
                "local_users": True,
                "hide_signup": False,
                "admin_email": "admin@example.org",
                "polling_secs": 15,
                "sso": True
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
        return (
            jsonify(REANAConfig.load("ui")),
            200,
        )
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
