# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server GitLab integration Flask-Blueprint."""

import logging
import traceback

import requests
from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    request,
    url_for,
)
from flask_login.utils import _create_identifier
from invenio_oauthclient.utils import get_safe_redirect_target
from itsdangerous import BadData, TimedJSONWebSignatureSerializer
from reana_commons.k8s.secrets import REANAUserSecretsStore
from werkzeug.local import LocalProxy

from reana_server.config import (
    REANA_GITLAB_OAUTH_APP_ID,
    REANA_GITLAB_OAUTH_APP_SECRET,
    REANA_GITLAB_URL,
    REANA_HOSTNAME,
)
from reana_server.decorators import signin_required
from reana_server.utils import (
    _format_gitlab_secrets,
    _get_gitlab_hook_id,
)


blueprint = Blueprint("gitlab", __name__)


serializer = LocalProxy(
    lambda: TimedJSONWebSignatureSerializer(current_app.config["SECRET_KEY"])
)


@blueprint.route("/gitlab/connect")
def gitlab_connect():
    r"""Endpoint to init the REANA connection to GitLab.

    ---
    get:
      summary: Initiate connection to GitLab.
      operationId: gitlab_connect
      description: >-
        Initiate connection to GitLab to authorize accessing the
        authenticated user's API.
      responses:
        302:
          description: >-
            Redirection to GitLab site.
    """
    # Get redirect target in safe manner.
    next_param = get_safe_redirect_target()
    # Create a JSON Web Token
    state_token = serializer.dumps({"next": next_param, "sid": _create_identifier(),})

    params = {
        "client_id": REANA_GITLAB_OAUTH_APP_ID,
        "redirect_uri": url_for(".gitlab_oauth", _external=True),
        "response_type": "code",
        "scope": "api",
        "state": state_token,
    }
    req = requests.PreparedRequest()
    req.prepare_url(REANA_GITLAB_URL + "/oauth/authorize", params)
    return redirect(req.url), 302


@blueprint.route("/gitlab", methods=["GET"])
@signin_required()
def gitlab_oauth(user):  # noqa
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
        if "code" in request.args:
            # Verifies state parameter and obtain next url
            state_token = request.args.get("state")
            assert state_token
            # Checks authenticity and integrity of state and decodes the value.
            state = serializer.loads(state_token)
            # Verifies that state is for this session and that next parameter
            # has not been modified.
            assert state["sid"] == _create_identifier()
            # Stores next URL
            next_url = state["next"]
            gitlab_code = request.args.get("code")
            params = {
                "client_id": REANA_GITLAB_OAUTH_APP_ID,
                "client_secret": REANA_GITLAB_OAUTH_APP_SECRET,
                "redirect_uri": url_for(".gitlab_oauth", _external=True),
                "code": gitlab_code,
                "grant_type": "authorization_code",
            }
            gitlab_response = requests.post(
                REANA_GITLAB_URL + "/oauth/token", data=params
            ).content
            secrets_store = REANAUserSecretsStore(str(user.id_))
            secrets_store.add_secrets(
                _format_gitlab_secrets(gitlab_response), overwrite=True
            )
            return redirect(next_url), 201
        else:
            return jsonify({"message": "OK"}), 200
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except (AssertionError, BadData):
        return jsonify({"message": "State param is invalid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/gitlab/projects", methods=["GET"])
@signin_required()
def gitlab_projects(user):  # noqa
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
        secrets_store = REANAUserSecretsStore(str(user.id_))
        gitlab_token = secrets_store.get_secret_value("gitlab_access_token")
        gitlab_user = secrets_store.get_secret_value("gitlab_user")
        gitlab_url = REANA_GITLAB_URL + "/api/v4/users/{0}/projects?access_token={1}"
        response = requests.get(gitlab_url.format(gitlab_user, gitlab_token))
        projects = dict()
        if response.status_code == 200:
            for gitlab_project in response.json():
                hook_id = _get_gitlab_hook_id(gitlab_project["id"], gitlab_token)
                projects[gitlab_project["id"]] = {
                    "name": gitlab_project["name"],
                    "path": gitlab_project["path_with_namespace"],
                    "url": gitlab_project["web_url"],
                    "hook_id": hook_id,
                }
            return jsonify(projects), 200
        return (
            jsonify({"message": "Project list could not be retrieved"}),
            response.status_code,
        )
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/gitlab/webhook", methods=["POST", "DELETE"])
def gitlab_webhook(user):  # noqa
    r"""Endpoint to setup a GitLab webhook.
    ---
    post:
      summary: Set a webhook on a user project from GitLab
      operationId: create_gitlab_webhook
      description: >-
        Setup a webhook for a GitLab project on GitLab.
      produces:
       - application/json
      parameters:
      - name: project_id
        in: path
        description: The GitLab project id.
        required: true
        type: integer
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
    delete:
      summary: Delete an existing webhook from GitLab
      operationId: delete_gitlab_webhook
      description: >-
        Remove an existing REANA webhook from a project on GitLab
      produces:
      - application/json
      parameters:
      - name: project_id
        in: path
        description: The GitLab project id.
        required: true
        type: integer
      - name: hook_id
        in: path
        description: The GitLab webhook id of the project.
        required: true
        type: integer
      responses:
        204:
          description: >-
            The webhook was properly deleted.
        404:
          description: >-
            No webhook found with provided id.
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
        secrets_store = REANAUserSecretsStore(str(user.id_))
        gitlab_token = secrets_store.get_secret_value("gitlab_access_token")
        parameters = request.json
        if request.method == "POST":
            gitlab_url = (
                REANA_GITLAB_URL + "/api/v4/projects/" + "{0}/hooks?access_token={1}"
            )
            webhook_payload = {
                "url": "https://{}/api/workflows".format(REANA_HOSTNAME),
                "push_events": True,
                "push_events_branch_filter": "master",
                "merge_requests_events": True,
                "enable_ssl_verification": False,
                "token": user.access_token,
            }
            webhook = requests.post(
                gitlab_url.format(parameters["project_id"], gitlab_token),
                data=webhook_payload,
            )
            return jsonify({"id": webhook.json()["id"]}), 201
        elif request.method == "DELETE":
            gitlab_url = (
                REANA_GITLAB_URL
                + "/api/v4/projects/"
                + "{0}/hooks/{1}?access_token={2}"
            )
            resp = requests.delete(
                gitlab_url.format(
                    parameters["project_id"], parameters["hook_id"], gitlab_token
                )
            )
            return resp.content, resp.status_code

    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
