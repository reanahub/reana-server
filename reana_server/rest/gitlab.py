# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021, 2022, 2023, 2024, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server GitLab integration Flask-Blueprint."""

import hmac
import logging
import secrets
import traceback
from datetime import timedelta
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
from reana_commons.k8s.secrets import UserSecretsStore
from reana_db.database import Session
from reana_db.models import User
from reana_db.secrets import get_or_create_bearer_secret
import marshmallow
from webargs import fields, validate
from webargs.flaskparser import use_kwargs


from reana_server.config import (
    REANA_GITLAB_OAUTH_APP_ID,
    REANA_GITLAB_OAUTH_APP_SECRET,
    REANA_GITLAB_URL,
)
from reana_server.decorators import signin_required
from reana_server.gitlab_client import (
    GitLabClient,
    GitLabClientRequestError,
    GitLabClientInvalidToken,
)
from reana_server.oauth_state import (
    GITLAB_STATE_COOKIE,
    InvalidOAuthState,
    clear_state_cookie,
    consume_state,
    issue_state,
    safe_next_url,
)
from reana_server.utils import (
    _format_gitlab_secrets,
    _get_gitlab_hook_id,
    naive_utcnow,
)

blueprint = Blueprint("gitlab", __name__)


def _gitlab_webhook_secret_expiry():
    """Return the expiry granted by a successful user authorization."""
    return naive_utcnow() + timedelta(
        seconds=current_app.config["REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME"]
    )


def _serialize_webhook_secret_status(user):
    """Return public metadata about a user's delegated webhook capability."""
    expires_at = user.gitlab_webhook_secret_expires_at
    return {
        "configured": bool(user.gitlab_webhook_secret),
        "expired": (
            bool(expires_at is None or expires_at <= naive_utcnow())
            if user.gitlab_webhook_secret
            else False
        ),
        "expires_at": expires_at.isoformat() + "Z" if expires_at else None,
        "max_lifetime_seconds": current_app.config[
            "REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME"
        ],
    }


@blueprint.route("/gitlab/webhook-token", methods=["GET", "PUT"])
@signin_required()
def gitlab_webhook_token(user):
    r"""Inspect or renew the delegated GitLab webhook capability.

    Renewal deliberately preserves the secret installed in existing GitLab
    projects. The authenticated OIDC request reauthorizes that capability for
    at most the operator-configured lifetime.

    ---
    get:
      summary: Get GitLab webhook authorization status
      operationId: get_gitlab_webhook_token
      description: >-
        Return expiry metadata for the current user's delegated GitLab
        webhook authorization. The secret itself is never returned.
      produces:
       - application/json
      responses:
        200:
          description: GitLab webhook authorization status.
          schema:
            type: object
            properties:
              configured:
                type: boolean
              expired:
                type: boolean
              expires_at:
                type: string
                format: date-time
                x-nullable: true
              max_lifetime_seconds:
                type: integer
        401:
          description: The request is not authenticated.
        500:
          description: The identity provider integration is not correctly configured.
          schema:
            type: object
            properties:
              message:
                type: string
        503:
          description: >-
            The identity provider or the authentication session store is
            temporarily unavailable.
        403:
          description: The authenticated user lacks the required REANA role.
    put:
      summary: Renew GitLab webhook authorization
      operationId: renew_gitlab_webhook_token
      description: >-
        Confirm the current user's REANA entitlement and extend the delegated
        GitLab webhook authorization without rotating its secret. Renewal does
        not re-enable a webhook that GitLab has disabled; the user must send a
        test delivery or re-enable it from that project's GitLab settings. If
        the authorization had already expired, the response includes a
        ``message`` pointing this out, since REANA cannot detect or repair a
        GitLab-side auto-disable on its own.
      produces:
       - application/json
      responses:
        200:
          description: Renewed GitLab webhook authorization status.
          schema:
            type: object
            properties:
              configured:
                type: boolean
              expired:
                type: boolean
              expires_at:
                type: string
                format: date-time
              max_lifetime_seconds:
                type: integer
              message:
                description: >-
                  Present only when the authorization had already expired,
                  warning that GitLab may have auto-disabled affected
                  webhooks and pointing to the manual recovery step.
                type: string
        404:
          description: No GitLab webhook secret is configured.
        401:
          description: The request is not authenticated.
        500:
          description: The identity provider integration is not correctly configured.
          schema:
            type: object
            properties:
              message:
                type: string
        503:
          description: >-
            The identity provider or the authentication session store is
            temporarily unavailable.
        403:
          description: The authenticated user lacks the required REANA role.
    """
    if request.method == "PUT":
        if not user.gitlab_webhook_secret:
            return jsonify(message="No GitLab webhook token is configured."), 404
        was_expired = (
            user.gitlab_webhook_secret_expires_at is None
            or user.gitlab_webhook_secret_expires_at <= naive_utcnow()
        )
        user.gitlab_webhook_secret_expires_at = _gitlab_webhook_secret_expiry()
        Session.commit()
        if was_expired:
            status = _serialize_webhook_secret_status(user)
            status["message"] = (
                "This authorization had expired, so GitLab may have "
                "automatically disabled webhooks using it. If your "
                "integration does not resume, send a test delivery or "
                "re-enable the webhook from each affected project's GitLab "
                "settings."
            )
            return jsonify(status), 200
    return jsonify(_serialize_webhook_secret_status(user)), 200


@blueprint.route("/gitlab/connect")
@signin_required()
def gitlab_connect(user):
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
        401:
          description: The request is not authenticated.
        403:
          description: The authenticated user lacks the required REANA role.
        500:
          description: The identity provider integration is not correctly configured.
          schema:
            type: object
            properties:
              message:
                type: string
        503:
          description: >-
            The identity provider or the authentication session store is
            temporarily unavailable.
    """
    # Get redirect target in safe manner.
    next_param = safe_next_url(request.args.get("next"))
    response = redirect("placeholder")
    state = issue_state(
        response,
        cookie_name=GITLAB_STATE_COOKIE,
        flow="gitlab",
        next=next_param,
        user_id=str(user.id_),
    )

    params = {
        "client_id": REANA_GITLAB_OAUTH_APP_ID,
        "redirect_uri": url_for(".gitlab_oauth", _external=True),
        "response_type": "code",
        "scope": "api",
        "state": state,
    }
    req = requests.PreparedRequest()
    req.prepare_url(REANA_GITLAB_URL + "/oauth/authorize", params)
    response.headers["Location"] = req.url
    return response, 302


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
        401:
          description: The request is not authenticated.
        503:
          description: >-
            The identity provider or the authentication session store is
            temporarily unavailable.
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
            # Verifies state parameter (signed state cookie) and obtains
            # the next url.
            state = consume_state(
                request.args.get("state", ""),
                cookie_name=GITLAB_STATE_COOKIE,
                expected_flow="gitlab",
            )
            if not hmac.compare_digest(state.get("user_id", ""), str(user.id_)):
                raise InvalidOAuthState("State param is invalid.")
            next_url = safe_next_url(state.get("next"))
            gitlab_code = request.args.get("code")
            params = {
                "client_id": REANA_GITLAB_OAUTH_APP_ID,
                "client_secret": REANA_GITLAB_OAUTH_APP_SECRET,
                "redirect_uri": url_for(".gitlab_oauth", _external=True),
                "code": gitlab_code,
                "grant_type": "authorization_code",
            }

            # request access token
            anonymous_gitlab_client = GitLabClient()
            gitlab_response = anonymous_gitlab_client.oauth_token(params).json()
            access_token = gitlab_response["access_token"]

            # get GitLab user details
            authenticated_gitlab_client = GitLabClient(access_token=access_token)
            gitlab_user = authenticated_gitlab_client.get_user().json()

            # store access token inside k8s secrets
            user_secrets = UserSecretsStore.fetch(user.id_)
            user_secrets.add_secrets(
                _format_gitlab_secrets(gitlab_user, access_token), overwrite=True
            )
            UserSecretsStore.update(user_secrets)
            response = redirect(next_url)
            return (
                clear_state_cookie(response, cookie_name=GITLAB_STATE_COOKIE),
                302,
            )
        else:
            return jsonify({"message": "OK"}), 200
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except InvalidOAuthState:
        return jsonify({"message": "State param is invalid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/gitlab/projects", methods=["GET"])
@use_kwargs(
    {
        "search": fields.Str(),
        "page": fields.Int(validate=validate.Range(min=1)),
        "size": fields.Int(validate=validate.Range(min=1)),
    },
    location="query",
    unknown=marshmallow.EXCLUDE,
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
        401:
          description: >-
            Request failed. The stored GitLab access token is not valid.
          schema:
            type: object
            properties:
              message:
                type: string
        503:
          description: >-
            The identity provider or the authentication session store is
            temporarily unavailable.
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
        params = {
            # show projects in which user is at least a `Maintainer`
            # as that's the minimum access level needed to create webhooks
            "min_access_level": 40,
            "search": search,
            # include ancestor namespaces when matching search criteria
            "search_namespaces": "true",
            # return only basic information about the projects
            "simple": "true",
        }

        gitlab_client = GitLabClient.from_k8s_secret(user.id_)
        gitlab_res = gitlab_client.get_projects(page=page, per_page=size, **params)

        projects = list()
        for gitlab_project in gitlab_res.json():
            hook_id = _get_gitlab_hook_id(gitlab_project["id"], gitlab_client)
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
    except GitLabClientInvalidToken as e:
        return jsonify({"message": str(e)}), 401
    except GitLabClientRequestError as e:
        logging.error(str(e))
        return (
            jsonify({"message": "Project list could not be retrieved"}),
            e.response.status_code,
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
        401:
          description: >-
            Request failed. The stored GitLab access token is not valid.
          schema:
            type: object
            properties:
              message:
                type: string
        409:
          description: >-
            The delegated GitLab webhook authorization has expired and must be
            renewed before enabling another project.
          schema:
            type: object
            properties:
              message:
                type: string
        503:
          description: >-
            The identity provider or the authentication session store is
            temporarily unavailable.
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
        401:
          description: >-
            Request failed. The stored GitLab access token is not valid.
          schema:
            type: object
            properties:
              message:
                type: string
        404:
          description: >-
            No webhook found with provided id.
        503:
          description: >-
            The identity provider or the authentication session store is
            temporarily unavailable.
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
        gitlab_client = GitLabClient.from_k8s_secret(user.id_)
        parameters = request.json
        if request.method == "POST":
            # Create the per-user webhook secret atomically. Two concurrent
            # first-time enables must not each generate and install a different
            # secret, which would leave one project configured with a value
            # REANA later rejects. get_or_create_bearer_secret locks the user
            # row so creation is serialised: the winner persists its secret
            # and the loser reuses that stored value instead of its own.
            webhook_secret, created = get_or_create_bearer_secret(
                Session,
                User,
                {"id_": user.id_},
                "gitlab_webhook_secret",
                lambda: secrets.token_urlsafe(32),
            )
            if created:
                # Same locked row as `user` (same session, same primary
                # key) -- setting the expiry here is still covered by the
                # lock get_or_create_bearer_secret held during creation.
                user.gitlab_webhook_secret_expires_at = _gitlab_webhook_secret_expiry()
            elif (
                not user.gitlab_webhook_secret_expires_at
                or user.gitlab_webhook_secret_expires_at <= naive_utcnow()
            ):
                Session.commit()  # release the row lock before returning
                return (
                    jsonify(
                        message=(
                            "GitLab webhook authorization has expired. "
                            "Renew it before enabling another project."
                        )
                    ),
                    409,
                )
            # Persist any newly created secret and release the row lock before
            # the GitLab network call, so the lock is never held across I/O.
            Session.commit()
            webhook_config = {
                "url": url_for("workflows.create_workflow", _external=True),
                "push_events": True,
                "push_events_branch_filter": "master",
                "merge_requests_events": True,
                "enable_ssl_verification": current_app.config[
                    "REANA_GITLAB_WEBHOOK_SSL_VERIFICATION"
                ],
                "token": webhook_secret,
            }
            webhook = gitlab_client.create_webhook(
                parameters["project_id"], webhook_config
            ).json()
            return jsonify({"id": webhook["id"]}), 201
        elif request.method == "DELETE":
            project_id = parameters["project_id"]
            hook_id = parameters["hook_id"]
            resp = gitlab_client.delete_webhook(project_id, hook_id)
            return resp.content, resp.status_code
    except GitLabClientInvalidToken as e:
        return jsonify({"message": str(e)}), 401
    except GitLabClientRequestError as e:
        logging.error(str(e))
        return (
            jsonify({"message": "Error while creating or deleting webhook"}),
            e.response.status_code,
        )
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
