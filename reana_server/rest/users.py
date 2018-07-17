# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017 CERN.
#
# REANA is free software; you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# REANA is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with REANA; if not, see <http://www.gnu.org/licenses>.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization or
# submit itself to any jurisdiction.

"""Reana-Server User Endpoints."""

from flask import Blueprint, jsonify, request
import logging
import traceback

from reana_commons.database import Session
from reana_commons.models import User

from reana_server.config import ADMIN_USER_ID
from reana_server.utils import _get_users, _create_user

blueprint = Blueprint('users', __name__)


@blueprint.route('/users', methods=['GET'])
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
        user_id = request.args.get('id_')
        user_email = request.args.get('email')
        user_token = request.args.get('user_token')
        access_token = request.args.get('access_token')
        users = _get_users(user_id, user_email, user_token, access_token)
        if users:
            users_response = []
            for user in users:
                user_response = dict(id_=user.id_,
                                     email=user.email,
                                     access_token=user.access_token)
                users_response.append(user_response)
            return jsonify(users_response), 200
        else:
            return jsonify({"message": "User {} does not exist.".
                            format(user_id)}, 404)
    except ValueError:
        return jsonify({"message": "Action not permitted."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/users', methods=['POST'])
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
        user_email = request.args.get('email')
        user_token = request.args.get('user_token')
        access_token = request.args.get('access_token')
        user = _create_user(user_email, user_token, access_token)
        return jsonify({"message": "User was successfully created.",
                        "id_": user.id_,
                        "email": user.email,
                        "access_token": user.access_token}), 201
    except ValueError:
        return jsonify({"message": "Action not permitted."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
