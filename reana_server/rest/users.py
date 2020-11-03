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

from reana_server import __version__
from reana_server.config import ADMIN_EMAIL, REANA_HOSTNAME
from reana_server.utils import (
    _create_user,
    _get_user_from_invenio_user,
    _get_users,
    get_user_from_token,
    JinjaEnv,
)

blueprint = Blueprint("users", __name__)


@blueprint.route("/users", methods=["GET"])
def get_user():  # noqa
    r"""Endpoint to get user information from the server.
    ---
    get:
      summary: Get user information. Requires the admin api key.
      description: >-
        Get user information.
      operationId: get_user
      produces:
        - application/json
      parameters:
        - name: email
          in: query
          description: Not required. The email of the user.
          required: false
          type: string
        - name: id_
          in: query
          description: Not required. UUID of the user.
          required: false
          type: string
        - name: user_token
          in: query
          description: Not required. API key of the admin.
          required: false
          type: string
        - name: access_token
          in: query
          description: Required. API key of the admin.
          required: true
          type: string
      responses:
        200:
          description: >-
            Users matching criteria were found.
            Returns all stored user information.
          schema:
            type: array
            items:
              type: object
              properties:
                id_:
                  type: string
                email:
                  type: string
                access_token:
                  type: string
          examples:
            application/json:
              [
                {
                  "id": "00000000-0000-0000-0000-000000000000",
                  "email": "user@reana.info",
                  "access_token": "Drmhze6EPcv0fN_81Bj-nA",
                },
                {
                  "id": "00000000-0000-0000-0000-000000000001",
                  "email": "user2@reana.info",
                  "access_token": "Drmhze6EPcv0fN_81Bj-nB",
                },
              ]
        403:
          description: >-
            Request failed. The incoming payload seems malformed.
        404:
          description: >-
            Request failed. User does not exist.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000 does not
                            exist."
              }
        500:
          description: >-
            Request failed. Internal server error.
          examples:
            application/json:
              {
                "message": "Error while querying."
              }
    """
    try:
        user_id = request.args.get("id_")
        user_email = request.args.get("email")
        user_token = request.args.get("user_token")
        access_token = request.args.get("access_token")
        users = _get_users(user_id, user_email, user_token, access_token)
        if users:
            users_response = []
            for user in users:
                user_response = dict(
                    id_=user.id_, email=user.email, access_token=user.access_token
                )
                users_response.append(user_response)
            return jsonify(users_response), 200
        else:
            return jsonify({"message": "User {} does not exist.".format(user_id)}, 404)
    except ValueError:
        return jsonify({"message": "Action not permitted."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/users", methods=["POST"])
def create_user():  # noqa
    r"""Endpoint to create users.

    ---
    post:
      summary: Creates a new user with the provided information.
      description: >-
        This resource creates a new user with the provided
        information (email, id). Requires the admin api key.
      operationId: create_user
      produces:
        - application/json
      parameters:
        - name: email
          in: query
          description: Required. The email of the user.
          required: true
          type: string
        - name: user_token
          in: query
          description: Required. API key of the user.
          required: false
          type: string
        - name: access_token
          in: query
          description: Required. API key of the admin.
          required: true
          type: string
      responses:
        201:
          description: >-
            User created successfully. Returns the access_token and a message.
          schema:
            type: object
            properties:
              id_:
                type: string
              email:
                type: string
              access_token:
                type: string
          examples:
            application/json:
              {
                "id_": "00000000-0000-0000-0000-000000000000",
                "email": "user@reana.info",
                "access_token": "Drmhze6EPcv0fN_81Bj-nA"
              }
        403:
          description: >-
            Request failed. The incoming payload seems malformed.
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
        user_email = request.args.get("email")
        user_token = request.args.get("user_token")
        access_token = request.args.get("access_token")
        user = _create_user(user_email, user_token, access_token)
        return (
            jsonify(
                {
                    "message": "User was successfully created.",
                    "id_": user.id_,
                    "email": user.email,
                    "access_token": user.access_token,
                }
            ),
            201,
        )
    except ValueError:
        return jsonify({"message": "Action not permitted."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


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
          examples:
            application/json:
              {
                "email": "user@reana.info",
                "reana_server_version": "0.7.0",
                "reana_token": {
                    "value": "Drmhze6EPcv0fN_81Bj-nA",
                    "status": "active",
                    "requested_at": "Mon, 25 May 2020 10:39:57 GMT",
                },
                "full_name": "John Doe",
                "username": "jdoe"
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
                        "reana_server_version": __version__,
                        "reana_token": {
                            "value": me.access_token,
                            "status": me.access_token_status,
                            "requested_at": me.latest_access_token.created
                            if me.latest_access_token
                            else None,
                        },
                        "full_name": me.full_name,
                        "username": me.username,
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
