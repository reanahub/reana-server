# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server GitLab Oauth Flask-Blueprint."""

import logging
import requests
import traceback

from flask import Blueprint, jsonify, request

from reana_server.config import GITLAB_APP_ID, GITLAB_APP_SECRET

blueprint = Blueprint('oauth', __name__)


@blueprint.route('/gitlab', methods=['GET'])
def gitlab_oauth():  # noqa
    r"""Endpoint to authenticate the user on GitLab.
    ---
    get:
      summary: Retrieve the user's GitLab access code.
      operationId: gitlab_oauth
      description: >-
        This resource receives a an authorization code from the
            GitLab OAuth API and makes a POST request to the same API
            to retrive the user's access token.
      produces:
       - application/json
      responses:
        200:
          description: >-
            Authorization succeeded.
          schema:
            type: object
            properties:
              access_token:
                type: string
              token_type:
                type: string
              refresh_token:
                type: string
              scope:
                type: string
              created_at:
                type: integer
           examples:
            application/json:
              {
                "access_token": "1234567abc",
                "token_type": "bearer",
                "refresh_token": "abc1234567",
                "scope": "api",
                "created_at": 1234567890
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        code = request.args.get('code')
        redirect_uri = 'https://leticia-imac.dyndns.cern.ch:5000/api/gitlab'
        parameters = 'client_id={0}&client_secret={1}&code={2}' + \
            '&grant_type=authorization_code&redirect_uri={3}'
        parameters = parameters.format(GITLAB_APP_ID, GITLAB_APP_SECRET,
                                       code, redirect_uri)
        gitlab_token_uri = "https://gitlab.com/oauth/token"
        gitlab_response = requests.post(url=gitlab_token_uri,
                                        data=parameters)._content
    except AttributeError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
    return gitlab_response, 200
