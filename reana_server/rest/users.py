# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server User Endpoints."""

import logging
import traceback

from bravado.exception import HTTPError
from flask import Blueprint, jsonify, request
from flask_login import current_user
from reana_db.models import AuditLogAction
from reana_commons.email import send_email
from reana_commons.errors import REANAEmailNotificationError

from reana_server.config import ADMIN_EMAIL, REANA_HOSTNAME
from reana_server.utils import (
    _get_user_from_invenio_user,
    get_user_from_token,
    JinjaEnv,
)

blueprint = Blueprint("users", __name__)


@blueprint.route("/you", methods=["GET"])
def get_you():
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
                        type: number
                      limit:
                        type: number
                  cpu:
                    type: object
                    properties:
                      usage:
                        type: number
                      limit:
                        type: number
          examples:
            application/json:
              {
                "email": "user@reana.info",
                "reana_token": {
                    "value": "Drmhze6EPcv0fN_81Bj-nA",
                    "status": "active",
                    "requested_at": "Mon, 25 May 2020 10:39:57 GMT",
                },
                "full_name": "John Doe",
                "username": "jdoe",
                "quota": {
                  "cpu": {
                    "limit": 200000,
                    "usage": 70536
                  },
                  "disk": {
                    "limit": 200000,
                    "usage": 408
                  }
                }
              }
        401:
          description: >-
            Error message indicating that the uses is not authenticated.
          schema:
            type: object
            properties:
              error:
                type: string
          examples:
            application/json:
              {
                "error": "User not logged in"
              }
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
            Request failed. Internal server error.
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """
    try:
        me = None
        if current_user.is_authenticated:
            me = _get_user_from_invenio_user(current_user.email)
        elif "access_token" in request.args:
            me = get_user_from_token(request.args.get("access_token"))
        if me:
            return (
                jsonify(
                    {
                        "email": me.email,
                        "reana_token": {
                            "value": me.access_token,
                            "status": me.access_token_status,
                            "requested_at": me.latest_access_token.created
                            if me.latest_access_token
                            else None,
                        },
                        "full_name": me.full_name,
                        "username": me.username,
                        "quota": me.get_quota_usage(),
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
def request_token():
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
              error:
                type: string
          examples:
            application/json:
              {
                "error": "User not logged in"
              }
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
            Request failed. Internal server error.
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """
    try:
        user = None
        if current_user.is_authenticated:
            user = _get_user_from_invenio_user(current_user.email)
        elif "access_token" in request.args:
            user = get_user_from_token(request.args.get("access_token"))
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
        )
        try:
            send_email(ADMIN_EMAIL, email_subject, email_body)
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
