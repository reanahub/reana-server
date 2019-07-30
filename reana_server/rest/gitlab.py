# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server GitLab integration Flask-Blueprint."""

import logging
import requests
import traceback

from flask import Blueprint, jsonify, request
from flask_login import current_user
from reana_commons.k8s.secrets import REANAUserSecretsStore
from reana_server.api_client import current_rwc_api_client
from reana_server.config import REANA_GITLAB_OAUTH_APP_ID, \
                                REANA_GITLAB_OAUTH_APP_SECRET, \
                                REANA_GITLAB_OAUTH_REDIRECT_URL, \
                                REANA_GITLAB_URL, \
                                REANA_URL
from reana_server.utils import get_user_from_token, \
                               _get_user_from_invenio_user, \
                               _format_gitlab_secrets

blueprint = Blueprint('gitlab', __name__)


@blueprint.route('/gitlab', methods=['GET'])
def gitlab_oauth():  # noqa
    r"""Endpoint to authorize REANA on GitLab.
    ---
    get:
      summary: Get access token from GitLab
      operationId: gitlab_oauth
      description: >-
        Authorize REANA on GitLab.
      produces:
       - application/json
      responses:
        200:
          description: >-
            Ping succeeded.
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
        201:
          description: >-
            Authorization succeeded. GitLab secret created.
          schema:
            type: object
            properties:
              message:
                type: string
              status:
                type: string
          examples:
            application/json:
              message: GitLab secret created
              status: 201
        403:
          description: >-
            Request failed. User token not valid.
          examples:
            application/json:
              {
                "message": "Token is not valid."
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        if current_user.is_authenticated:
            user = _get_user_from_invenio_user(current_user.email)
        else:
            user = get_user_from_token(request.args.get('access_token'))
        if 'code' in request.args:
            gitlab_code = request.args.get('code')
            parameters = "client_id={0}&" + \
                         "client_secret={1}&code={2}&" + \
                         "grant_type=authorization_code&redirect_uri={3}"
            parameters = parameters.format(REANA_GITLAB_OAUTH_APP_ID,
                                           REANA_GITLAB_OAUTH_APP_SECRET,
                                           gitlab_code,
                                           REANA_GITLAB_OAUTH_REDIRECT_URL)
            gitlab_response = requests.post(REANA_GITLAB_URL + '/oauth/token',
                                            data=parameters).content
            secrets_store = REANAUserSecretsStore(str(user.id_))
            secrets_store.add_secrets(_format_gitlab_secrets(gitlab_response),
                                      overwrite=True)
            return jsonify({"message": "GitLab secret created"}), 201
        else:
            return jsonify({"message": "OK"}), 200
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/gitlab/projects', methods=['GET'])
def gitlab_projects():  # noqa
    r"""Endpoint to retrieve GitLab projects.
    ---
    get:
      summary: Get user project from GitLab
      operationId: gitlab_projects
      description: >-
        Retrieve projects from GitLab.
      produces:
       - application/json
      responses:
        200:
          description: >-
            This resource return all projects owned by
            the user on GitLab in JSON format.
        403:
          description: >-
            Request failed. User token not valid.
          examples:
            application/json:
              {
                "message": "Token is not valid."
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        if current_user.is_authenticated:
            user = _get_user_from_invenio_user(current_user.email)
        else:
            user = get_user_from_token(request.args.get('access_token'))
        secrets_store = REANAUserSecretsStore(str(user.id_))
        gitlab_token = secrets_store.get_secret_value('gitlab_access_token')
        gitlab_user = secrets_store.get_secret_value('gitlab_user')
        gitlab_url = REANA_GITLAB_URL + \
            "/api/v4/users/{0}/projects?access_token={1}"
        projects = requests.get(gitlab_url.format(gitlab_user, gitlab_token))
        return projects.content, 200
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/gitlab/webhook', methods=['POST'])
def gitlab_webhook():  # noqa
    r"""Endpoint to setup a GitLab webhook.
    ---
    post:
      summary: Set a webhook on a user project from GitLab
      operationId: gitlab_webhook
      description: >-
        Setup a webhook for a GitLab project on GitLab.
      produces:
       - application/json
      responses:
      201:
        description: >-
          The webhook was created.
      403:
        description: >-
          Request failed. User token not valid.
        examples:
          application/json:
            {
              "message": "Token is not valid."
            }
      500:
        description: >-
          Request failed. Internal controller error.
    """
    try:
        if current_user.is_authenticated:
            user = _get_user_from_invenio_user(current_user.email)
        else:
            user = get_user_from_token(request.args.get('access_token'))
        secrets_store = REANAUserSecretsStore(str(user.id_))
        gitlab_token = secrets_store.get_secret_value('gitlab_access_token')
        parameters = request.json
        gitlab_url = REANA_GITLAB_URL + "/api/v4/projects/" + \
            "{0}/hooks?access_token={1}"
        webhook_payload = {
            "url": REANA_URL + "/api/workflows",
            "push_events": True,
            "push_events_branch_filter": "master",
            "merge_requests_events": True,
            "enable_ssl_verification": False,
            "token": user.access_token,
        }
        webhook = requests.post(gitlab_url.format(
                                              parameters['project_id'],
                                              gitlab_token),
                                data=webhook_payload)
        return webhook.content, 201
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
