# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server config-functionality Flask-Blueprint."""

import logging
import traceback

from flask import Blueprint, jsonify

from reana_server.config import (
    REANA_UI_ANNOUNCEMENT,
    REANA_UI_POOLING_SECS,
    REANA_UI_DOCS_URL,
    REANA_UI_FORUM_URL,
    REANA_UI_MATTERMOST_URL,
    REANA_UI_CERN_SSO,
    REANA_UI_LOCAL_USERS,
)


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
                "pooling_secs": 15,
                "docs_url": "http://docs.reana.io/",
                "forum_url": "https://forum.reana.io/",
                "mattermost_url": "https://mattermost.web.cern.ch/it-dep/channels/reana",
                "sso": True,
                "local_users": True
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
        return (
            jsonify(
                {
                    "announcement": REANA_UI_ANNOUNCEMENT,
                    "pooling_secs": REANA_UI_POOLING_SECS,
                    "docs_url": REANA_UI_DOCS_URL,
                    "forum_url": REANA_UI_FORUM_URL,
                    "mattermost_url": REANA_UI_MATTERMOST_URL,
                    "cern_sso": REANA_UI_CERN_SSO,
                    "local_users": REANA_UI_LOCAL_USERS,
                }
            ),
            200,
        )
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
