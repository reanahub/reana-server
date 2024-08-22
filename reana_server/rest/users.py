# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021, 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server User Endpoints."""

import logging
import traceback

from bravado.exception import HTTPError
from flask import Blueprint, jsonify
from reana_db.database import Session
from reana_db.models import AuditLogAction, User, UserWorkflow, Workflow
from reana_commons.config import (
    REANA_COMPONENT_PREFIX,
    REANA_INFRASTRUCTURE_KUBERNETES_NAMESPACE,
)
from reana_commons.email import send_email, REANA_EMAIL_RECEIVER
from reana_commons.errors import REANAEmailNotificationError

from reana_server import __version__
from reana_server.config import REANA_HOSTNAME
from reana_server.decorators import signin_required
from reana_server.utils import JinjaEnv


blueprint = Blueprint("users", __name__)


@blueprint.route("/you", methods=["GET"])
@signin_required(token_required=False)
def get_you(user):
    r"""Endpoint to get user information.

    ---
    get:
      summary: Gets information about authenticated user.
      description: >-
        This resource provides basic information about an authenticated
        user based on the session cookie presence.
      operationId: get_you
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
            User information correspoding to the session cookie sent
            in the request.
          schema:
            type: object
            properties:
              email:
                type: string
              reana_server_version:
                type: string
              reana_token:
                type: object
                properties:
                  value:
                    type: string
                  status:
                    type: string
                  requested_at:
                    type: string
              quota:
                type: object
                properties:
                  disk:
                    type: object
                    properties:
                      usage:
                        type: object
                        properties:
                          raw:
                            type: number
                          human_readable:
                            type: string
                      limit:
                        type: object
                        properties:
                          raw:
                            type: number
                          human_readable:
                            type: string
                      health:
                        type: string
                  cpu:
                    type: object
                    properties:
                      usage:
                        type: object
                        properties:
                          raw:
                            type: number
                          human_readable:
                            type: string
                      limit:
                        type: object
                        properties:
                          raw:
                            type: number
                          human_readable:
                            type: string
                      health:
                        type: string
          examples:
            application/json:
              {
                "email": "user@reana.info",
                "reana_server_version": "0.8.1",
                "reana_token": {
                    "value": "Drmhze6EPcv0fN_81Bj-nA",
                    "status": "active",
                    "requested_at": "Mon, 25 May 2020 10:39:57 GMT",
                },
                "full_name": "John Doe",
                "username": "jdoe",
                "quota": {
                  "cpu": {
                    "limit": {
                      "raw": 200000,
                      "human_readable": "3m 20s"
                    },
                    "usage": {
                      "raw": 70536,
                      "human_readable": "1m 10s"
                    },
                    "health": "healthy"
                  },
                  "disk": {
                    "limit": {
                      "raw": 52430000,
                      "human_readable": "50 MB"
                    },
                    "usage": {
                      "raw": 784384,
                      "human_readable": "766 KB"
                    },
                    "health": "healthy"
                  }
                }
              }
        401:
          description: >-
            Error message indicating that the uses is not authenticated.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User not logged in"
              }
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
        if user:
            return (
                jsonify(
                    {
                        "id_": user.id_,
                        "email": user.email,
                        "reana_server_version": __version__,
                        "reana_token": {
                            "value": user.access_token,
                            "status": user.access_token_status,
                            "requested_at": (
                                user.latest_access_token.created
                                if user.latest_access_token
                                else None
                            ),
                        },
                        "full_name": user.full_name,
                        "username": user.username,
                        "quota": user.get_quota_usage(),
                    }
                ),
                200,
            )
        return jsonify(message="User not logged in"), 401
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/token", methods=["PUT"])
@signin_required(token_required=False)
def request_token(user):
    r"""Endpoint to request user access token.

    ---
    put:
      summary: Requests a new access token for the authenticated user.
      description: >-
        This resource allows the user to create an empty REANA access token
        and mark it as requested.
      operationId: request_token
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
            User information correspoding to the session cookie sent
            in the request.
          schema:
            type: object
            properties:
              reana_token:
                type: object
                properties:
                  status:
                    type: string
                  requested_at:
                    type: string
          examples:
            application/json:
              {
                "reana_token": {
                  "status": "requested",
                  "requested_at": "Mon, 25 May 2020 10:45:15 GMT"
                }
              }
        401:
          description: >-
            Error message indicating that the uses is not authenticated.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User not logged in"
              }
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
        user.request_access_token()
        user.log_action(AuditLogAction.request_token)
        email_subject = f"[{REANA_HOSTNAME}] Token request ({user.email})"
        fields = [
            "id_",
            "email",
            "full_name",
            "username",
            "access_token",
            "access_token_status",
        ]
        user_data = "\n".join([f"{f}: {getattr(user, f, None)}" for f in fields])
        email_body = JinjaEnv.render_template(
            "emails/token_request.txt",
            user_data=user_data,
            user_email=user.email,
            reana_hostname=REANA_HOSTNAME,
            namespace=REANA_INFRASTRUCTURE_KUBERNETES_NAMESPACE,
            component_prefix=REANA_COMPONENT_PREFIX,
        )
        try:
            send_email(REANA_EMAIL_RECEIVER, email_subject, email_body)
        except REANAEmailNotificationError:
            logging.error(traceback.format_exc())

        return (
            jsonify(
                {
                    "reana_token": {
                        "status": user.access_token_status,
                        "requested_at": user.latest_access_token.created,
                    }
                }
            ),
            200,
        )

    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/users/shared-with-you", methods=["GET"])
@signin_required()
def get_users_shared_with_you(user):
    r"""Endpoint to get users that shared workflow(s) with the authenticated user.

    ---
    get:
      summary: Gets users that shared workflow(s) with the authenticated user.
      description: >-
        This resource provides information about users that shared
        workflow(s) with the authenticated user.
      operationId: get_users_shared_with_you
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
            Users that shared workflow(s) with the authenticated user.
          schema:
            type: object
            properties:
              users:
                type: array
                items:
                  type: object
                  properties:
                    email:
                      type: string
          examples:
            application/json:
              {
                "users_shared_with_you": [
                  {
                    "email": "john.doe@example.org",
                    }
                ]
            }
        401:
          description: >-
            Error message indicating that the uses is not authenticated.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User not logged in"
              }
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
        shared_workflows_ids = (
            Session.query(UserWorkflow.workflow_id)
            .filter(UserWorkflow.user_id == user.id_)
            .subquery()
        )

        shared_workflow_owners_ids = (
            Session.query(Workflow.owner_id)
            .filter(Workflow.id_.in_(shared_workflows_ids))
            .subquery()
        )

        users = (
            Session.query(User.email)
            .filter(User.id_.in_(shared_workflow_owners_ids))
            .all()
        )

        response = {"users_shared_with_you": [{"email": user.email} for user in users]}
        return jsonify(response), 200
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/users/you-shared-with", methods=["GET"])
@signin_required()
def get_users_you_shared_with(user):
    r"""Endpoint to get users that the authenticated user shared workflow(s) with.

    ---
    get:
      summary: Gets users that the authenticated user shared workflow(s) with.
      description: >-
        This resource provides information about users that the authenticated user
        shared workflow(s) with.
      operationId: get_users_you_shared_with
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
            Users that the authenticated user shared workflow(s) with.
          schema:
            type: object
            properties:
              users:
                type: array
                items:
                  type: object
                  properties:
                    email:
                      type: string
          examples:
            application/json:
              {
                "users_you_shared_with": [
                  {
                    "email": "john.doe@example.org",
                    }
                ]
            }
        401:
          description: >-
            Error message indicating that the uses is not authenticated.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User not logged in"
              }
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
        owned_workflows_ids = (
            Session.query(Workflow.id_).filter(Workflow.owner_id == user.id_).subquery()
        )

        users_you_shared_with_ids = (
            Session.query(UserWorkflow.user_id)
            .filter(UserWorkflow.workflow_id.in_(owned_workflows_ids))
            .distinct()
            .subquery()
        )

        users = (
            Session.query(User.email)
            .filter(User.id_.in_(users_you_shared_with_ids))
            .all()
        )

        response = {"users_you_shared_with": [{"email": user.email} for user in users]}
        return jsonify(response), 200
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
