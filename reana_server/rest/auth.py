# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server auth Flask-Blueprint."""

import logging

from flask import Blueprint, jsonify

from reana_server.auth.discovery import get_openid_configuration
from reana_server.auth.errors import AuthError
from reana_server.config import REANA_AUTH

blueprint = Blueprint("auth", __name__)


@blueprint.route("/.well-known/openid-configuration", methods=["GET"])
def openid_configuration():
    r"""Get the trusted issuer's OpenID configuration.

    ---
    get:
      summary: Get the trusted issuer's OpenID configuration.
      description: >-
        Relays the OIDC discovery document of the deployment's trusted
        issuer, extended with the public client id that reana-client must
        use for the device authorization grant. This lets clients discover
        the identity provider knowing only the REANA URL.
      operationId: get_openid_configuration
      produces:
        - application/json
      responses:
        200:
          description: >-
            Request succeeded. The response contains the issuer's OpenID
            configuration and the REANA CLI client id.
          schema:
            type: object
            properties:
              issuer:
                type: string
              device_authorization_endpoint:
                type: string
              authorization_endpoint:
                type: string
              token_endpoint:
                type: string
              userinfo_endpoint:
                type: string
              jwks_uri:
                type: string
              reana_client_id:
                type: string
        502:
          description: >-
            Request failed. The issuer's OpenID configuration could not be
            fetched.
          schema:
            type: object
            properties:
              message:
                type: string
    """
    try:
        configuration = dict(get_openid_configuration())
    except AuthError as error:
        logging.error("Could not relay OpenID configuration: %s", error)
        return (
            jsonify(message="Could not fetch the issuer's OpenID configuration."),
            502,
        )
    configuration["reana_client_id"] = REANA_AUTH["cli_client_id"]
    return jsonify(configuration), 200
