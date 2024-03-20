# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021, 2022, 2023, 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server GitLab integration Flask-Blueprint."""

import logging
import traceback
from typing import Optional
from urllib.parse import urljoin

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
from webargs import fields, validate
from webargs.flaskparser import use_kwargs


from reana_server.config import (
    REANA_GITLAB_OAUTH_APP_ID,
    REANA_GITLAB_OAUTH_APP_SECRET,
    REANA_GITLAB_URL,
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
@signin_required()
def gitlab_connect(**kwargs):
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
    state_token = serializer.dumps(
        {
            "next": next_param,
            "sid": _create_identifier(),
        }
    )

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
       - text/html
      responses:
        200:
          description: >-
            Ping succeeded.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "OK"
              }
        302:
          description: >-
            Authorization succeeded. GitLab secret created.
        403:
          description: >-
            Request failed. User token not valid.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Token is not valid."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
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
            return redirect(next_url), 302
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
@use_kwargs(
    {
        "search": fields.Str(location="query"),
        "page": fields.Int(validate=validate.Range(min=1), location="query"),
        "size": fields.Int(validate=validate.Range(min=1), location="query"),
    }
)
@signin_required()
def gitlab_projects(
    user, search: Optional[str] = None, page: int = 1, size: Optional[int] = None
):  # noqa
    r"""Endpoint to retrieve GitLab projects.
    ---
    get:
      summary: Get user project from GitLab
      operationId: gitlab_projects
      description: >-
        Retrieve projects from GitLab.
      produces:
       - application/json
      parameters:
        - name: access_token
          in: query
          description: The API access_token of the current user.
          required: false
          type: string
        - name: search
          in: query
          description: The search string to filter the project list.
          required: false
          type: string
        - name: page
          in: query
          description: Results page number (pagination).
          required: false
          type: integer
        - name: size
          in: query
          description: Number of results per page (pagination).
          required: false
          type: integer
      responses:
        200:
          description: >-
            This resource return all projects owned by
            the user on GitLab in JSON format.
          schema:
            type: object
            properties:
              has_next:
                type: boolean
              has_prev:
                type: boolean
              page:
                type: integer
              size:
                type: integer
              total:
                type: integer
                x-nullable: true
              items:
                type: array
                items:
                  type: object
                  properties:
                    id:
                      type: integer
                    name:
                      type: string
                    path:
                      type: string
                    url:
                      type: string
                    hook_id:
                      type: integer
                      x-nullable: true
        403:
          description: >-
            Request failed. User token not valid.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Token is not valid."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        secrets_store = REANAUserSecretsStore(str(user.id_))
        gitlab_token = secrets_store.get_secret_value("gitlab_access_token")

        if not gitlab_token:
            return jsonify({"message": "Missing GitLab access token."}), 401

        gitlab_url = urljoin(REANA_GITLAB_URL, "/api/v4/projects")
        params = {
            "access_token": gitlab_token,
            # show projects in which user is at least a `Maintainer`
            # as that's the minimum access level needed to create webhooks
            "min_access_level": 40,
            "page": page,
            "per_page": size,
            "search": search,
            # include ancestor namespaces when matching search criteria
            "search_namespaces": "true",
            # return only basic information about the projects
            "simple": "true",
        }

        gitlab_res = requests.get(gitlab_url, params=params)
        if gitlab_res.status_code == 200:
            projects = list()
            for gitlab_project in gitlab_res.json():
                hook_id = _get_gitlab_hook_id(gitlab_project["id"], gitlab_token)
                projects.append(
                    {
                        "id": gitlab_project["id"],
                        "name": gitlab_project["name"],
                        "path": gitlab_project["path_with_namespace"],
                        "url": gitlab_project["web_url"],
                        "hook_id": hook_id,
                    }
                )

            response = {
                "has_next": bool(gitlab_res.headers.get("x-next-page")),
                "has_prev": bool(gitlab_res.headers.get("x-prev-page")),
                "items": projects,
                "page": int(gitlab_res.headers.get("x-page")),
                "size": int(gitlab_res.headers.get("x-per-page")),
                "total": (
                    int(gitlab_res.headers.get("x-total"))
                    if gitlab_res.headers.get("x-total")
                    else None
                ),
            }

            return jsonify(response), 200
        return (
            jsonify({"message": "Project list could not be retrieved"}),
            gitlab_res.status_code,
        )
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/gitlab/webhook", methods=["POST", "DELETE"])
@signin_required()
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
        - name: data
          in: body
          description: Data required to set a new webhook from GitLab.
          schema:
            required:
              - project_id
            type: object
            properties:
              project_id:
                description: The GitLab project id.
                type: string
      responses:
        201:
          description: >-
            The webhook was created.
        403:
          description: >-
            Request failed. User token not valid.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Token is not valid."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    delete:
      summary: Delete an existing webhook from GitLab
      operationId: delete_gitlab_webhook
      description: >-
        Remove an existing REANA webhook from a project on GitLab
      produces:
      - application/json
      parameters:
        - name: data
          in: body
          description: Data required to delete an existing webhook from GitLab.
          schema:
            type: object
            required:
              - project_id
              - hook_id
            properties:
              project_id:
                description: The GitLab project id.
                type: string
              hook_id:
                description: The GitLab webhook id of the project.
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
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Token is not valid."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
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
                "url": url_for("workflows.create_workflow", _external=True),
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
