# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020, 2021, 2022, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server config-functionality Flask-Blueprint."""

import logging
import traceback

from flask import Blueprint, current_app, jsonify
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
      security: []
      produces:
        - application/json
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
                "admin_email": "admin@example.org",
                "polling_secs": 15,
                "auth": {
                  "bff_enabled": True,
                  "login_url": "/api/login",
                  "logout_url": "/api/logout"
                }
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
        ui_config = dict(REANAConfig.load("ui") or {})
        # Browser-authentication endpoints consumed by the web UI.
        auth_config = current_app.config["REANA_AUTH"]
        ui_config["auth"] = {
            "bff_enabled": bool(auth_config["bff_enabled"] and auth_config["issuer"]),
            "login_url": "/api/login",
            "logout_url": "/api/logout",
        }
        return (
            jsonify(ui_config),
            200,
        )
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
