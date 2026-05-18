# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server User Endpoints."""

import logging
import secrets
import traceback

from bravado.exception import HTTPError
from flask import Blueprint, Response, jsonify, request
from marshmallow import Schema, ValidationError, fields
from reana_db.database import Session
from reana_db.models import AuditLogAction, ResourceType, User, UserWorkflow, Workflow
from reana_commons.config import (
    REANA_COMPONENT_PREFIX,
    REANA_INFRASTRUCTURE_KUBERNETES_NAMESPACE,
)
from reana_commons.email import send_email, REANA_EMAIL_RECEIVER
from reana_commons.errors import REANAEmailNotificationError

from reana_server import __version__
from reana_server.config import (
    REANA_HOSTNAME,
    REANA_TOKEN_MANAGEMENT_SECRET,
)
from reana_server.decorators import signin_required
from reana_server.utils import (
    JinjaEnv,
    _get_admin_user_or_raise,
    _get_user_by_criteria,
    revoke_access_token_of_user,
    serialize_utc_datetime,
)

blueprint = Blueprint("users", __name__)


class DeleteTokenBodySchema(Schema):
    """Schema for delete_token endpoint body."""

    user_id = fields.Str()
    email = fields.Str()


def _management_response(status_code: int, message: str, **payload):
    """Build a management endpoint response including HTTP status in the body."""
    return jsonify(message=message, status=status_code, **payload), status_code


def _serialize_user_quota(user):
    """Serialize user quota usage including periodic CPU metadata."""
    quota = user.get_quota_usage()
    cpu_quota = quota.get(ResourceType.cpu.name)
    cpu_user_resource = next(
        (
            resource
            for resource in user.resources
            if resource.resource.type_ == ResourceType.cpu
        ),
        None,
    )
    if cpu_quota and cpu_user_resource:
        quota[ResourceType.cpu.name] = {
            **cpu_quota,
            "quota_period_months": cpu_user_resource.quota_period_months,
            "quota_period_start_at": serialize_utc_datetime(
                cpu_user_resource.quota_period_start_at
            ),
        }
    return quota


def _check_token_management_secret() -> tuple[Response, int] | None:
    if not REANA_TOKEN_MANAGEMENT_SECRET:
        return _management_response(403, "Token management endpoint is not configured.")

    secret = request.headers.get("X-Token-Management-Secret", "")
    if not secrets.compare_digest(secret, REANA_TOKEN_MANAGEMENT_SECRET):
        return _management_response(401, "Unauthorized")

    return None


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
                      quota_period_months:
                        type: integer
                        x-nullable: true
                        description: Length of the active CPU accounting window in months. `null` if periodic accounting is disabled.
                      quota_period_start_at:
                        type: string
                        format: date-time
                        x-nullable: true
                        description: Start timestamp of the active CPU accounting window. `null` if periodic accounting is disabled.
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
                    "health": "healthy",
                    "quota_period_months": 3,
                    "quota_period_start_at": "2026-04-01T13:06:32.992595Z"
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
                        "quota": _serialize_user_quota(user),
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


@blueprint.route("/token", methods=["DELETE"])
def delete_token():
    r"""Endpoint to revoke user access token via management secret.

    ---
    delete:
      summary: Revokes the active access token of the selected user.
      description: >-
        This management resource revokes the currently active REANA access token
        of a selected user. The endpoint is disabled unless
        `REANA_TOKEN_MANAGEMENT_SECRET` is configured.
      operationId: delete_token
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: X-Token-Management-Secret
          in: header
          description: REANA user token management secret
          required: true
          type: string
        - name: data
          in: body
          description: Data required to identify the target user (exactly one of `user_id` or `email` must be provided).
          required: true
          schema:
            type: object
            properties:
              user_id:
                type: string
                description: ID of the target user (mutually exclusive with `email`)
              email:
                type: string
                description: Email of the target user (mutually exclusive with `user_id`)
      responses:
        200:
          description: Access token successfully revoked.
          schema:
            type: object
            properties:
              status:
                type: integer
              id_:
                type: string
              email:
                type: string
              message:
                type: string
              reana_token:
                type: object
                properties:
                  status:
                    type: string
          examples:
            application/json:
              {
                "status": 200,
                "id_": "aa37d63d-3186-45d5-aa40-5d221cb170bf",
                "email": "john.doe@example.org",
                "message": "Access token revoked.",
                "reana_token": {
                  "status": "revoked"
                }
              }
        400:
          description: Invalid request.
          schema:
            type: object
            properties:
              status:
                type: integer
              message:
                type: string
        401:
          description: Unauthorized.
          schema:
            type: object
            properties:
              status:
                type: integer
              message:
                type: string
        403:
          description: Token management endpoint is not configured.
          schema:
            type: object
            properties:
              status:
                type: integer
              message:
                type: string
        404:
          description: No active token to revoke for the given user.
          schema:
            type: object
            properties:
              status:
                type: integer
              message:
                type: string
        500:
          description: Internal server error.
          schema:
            type: object
            properties:
              status:
                type: integer
              message:
                type: string
    """
    response = _check_token_management_secret()
    if response:
        return response

    json_body = request.get_json(silent=True)
    if not isinstance(json_body, dict):
        return _management_response(
            400, "Invalid request. Expected application/json body."
        )

    try:
        json_body = DeleteTokenBodySchema().load(json_body)
    except ValidationError as e:
        return _management_response(400, f"Invalid request. Errors: {e.messages}")

    user_id = json_body.get("user_id")
    email = json_body.get("email")
    if bool(user_id) == bool(email):
        return _management_response(
            400, "Exactly one of `user_id` or `email` must be provided."
        )

    user = _get_user_by_criteria(user_id, email)
    if not user:
        return _management_response(
            404, "No active token to revoke for the given user."
        )

    try:
        admin = _get_admin_user_or_raise(
            requested_via="reana_server.rest.users.delete_token"
        )
        revoke_access_token_of_user(
            user,
            revoked_by=admin,
            send_notification_email=True,
            include_token_in_log=False,
            requested_via="reana_server.rest.users.delete_token",
        )
    except REANAEmailNotificationError:
        logging.error(traceback.format_exc())
    except ValueError:
        return _management_response(
            404, "No active token to revoke for the given user."
        )
    except Exception as e:
        logging.error(traceback.format_exc())
        return _management_response(500, str(e))

    return _management_response(
        200,
        "Access token revoked.",
        id_=str(user.id_),
        email=user.email,
        reana_token={"status": user.access_token_status},
    )


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
