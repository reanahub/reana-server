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
      responses:
        '200':
          description: >-
            OpenID configuration
        '404':
          description: OpenID configuration not found
        '500':
          description: Internal server error
    """
    try:
        url = REANA_AUTH["openid"]["config_url"]

        if not url:
            return jsonify({"message": "OpenID configuration URL is not set"}), 500
        response = requests.get(url)
        if response.status_code == 404:
            return jsonify({"message": "OpenID configuration not found"}), 404
        response.raise_for_status()
        return jsonify(response.json()), 200
    except requests.RequestException:
        return jsonify({"message": "Failed to fetch OpenID configuration"}), 502
