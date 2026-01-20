# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2020, 2021, 2022, 2023, 2024, 2025 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server auth Flask-Blueprint."""

import requests
from flask import Blueprint, jsonify

from reana_server.config import REANA_AUTH

blueprint = Blueprint("auth", __name__)


@blueprint.route("/.well-known/openid-configuration", methods=["GET"])
def get_openid_configuration():
    r"""Get OpenID Configuration.

    ---
    get:
      summary: Get OpenID Configuration
      description: >-
        Returns the OpenID configuration for the REANA server.
      operationId: get_openid_configuration
      produces:
       - application/json
      responses:
        200:
          description: >-
            OpenID configuration
          schema:
            type: object
            properties:
              device_authorization_endpoint:
                type: string
              authorization_endpoint:
                type: string
              token_endpoint:
                type: string
              reana_client_id:
                type: string
        '404':
          description: OpenID configuration not found
        '500':
          description: Internal server error
    """
    try:
        # TODO The env location will be changed after `reana-server` jwt PR is merged and env variable structure agreed
        url = REANA_AUTH["openid"]["config_url"]

        if not url:
            return jsonify({"message": "OpenID configuration URL is not set"}), 500
        response = requests.get(url)
        if response.status_code == 404:
            return jsonify({"message": "OpenID configuration not found"}), 404
        response.raise_for_status()
        openid_config = response.json()
        openid_config["reana_client_id"] = REANA_AUTH["client_id"]

        return jsonify(openid_config), 200
    except requests.RequestException:
        return jsonify({"message": "Failed to fetch OpenID configuration"}), 502
