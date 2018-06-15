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
import secrets
import traceback

from reana_commons.database import Session
from reana_commons.models import User

from reana_server.config import ADMIN_USER_ID

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
          description: Required. The email of the user.
          required: true
          type: string
        - name: id_
          in: query
          description: Not required. UUID of the user.
          required: false
          type: string
        - name: token
          in: query
          description: Required. API key of the admin.
          required: true
          type: string
      responses:
        200:
          description: >-
            User was found. Returns all stored user information.
          schema:
            type: object
            properties:
              id_:
                type: string
              email:
                type: string
              token:
                type: string
          examples:
            application/json:
              {
                "id": "00000000-0000-0000-0000-000000000000",
                "email": "user@reana.info",
                "token": "Drmhze6EPcv0fN_81Bj-nA",
              }
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
        token = request.args.get('token')
        admin = Session.query(User).filter_by(id_=ADMIN_USER_ID).one_or_none()
        if token != admin.api_key:
            return jsonify({"message": "Action not permitted."}), 403
        search_criteria = dict()
        if user_id:
            search_criteria['id_'] = user_id
        if user_email:
            search_criteria['email'] = user_email
        user = Session.query(User).filter_by(**search_criteria).one_or_none()
        if user:
            return jsonify(id_=user.id_,
                           email=user.email,
                           token=user.api_key), 200
        else:
            return jsonify({"message": "User {} does not exist.".
                           format(user_id)}, 404)
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/users', methods=['POST'])
def create_user(): # noqa
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
        - name: token
          in: query
          description: Required. API key of the admin.
          required: true
          type: string
      responses:
        201:
          description: >-
            User created successfully. Returns the token and a message.
          schema:
            type: object
            properties:
              id_:
                type: string
              email:
                type: string
              token:
                type: string
          examples:
            application/json:
              {
                "id_": "00000000-0000-0000-0000-000000000000",
                "email": "user@reana.info",
                "token": "Drmhze6EPcv0fN_81Bj-nA"
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
        token = request.args.get('token')
        admin = Session.query(User).filter_by(id_=ADMIN_USER_ID).one_or_none()
        if token != admin.api_key:
            return jsonify({"message": "Action not permitted."}), 403
        user_parameters = dict(api_key=secrets.token_urlsafe())
        user_parameters['email'] = user_email
        user = User(**user_parameters)
        Session.add(user)
        Session.commit()

        return jsonify({"message": "User was successfully created.",
                        "id_": user.id_,
                        "email": user.email,
                        "token": user.api_key}), 201

    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/register', methods=['POST'])
def register_user(): # noqa
    r"""Endpoint to register users.

    ---
    post:
      summary: Registers a new user with the provided information.
      description: >-
        This resource registers a new user with the provided
        information email.
      operationId: register_user
      produces:
        - application/json
      parameters:
        - name: email
          in: query
          description: Required. The email of the user.
          required: true
          type: string
      responses:
        201:
          description: >-
            User registered successfully. Returns the token and a message.
          schema:
            type: object
            properties:
              id_:
                type: string
              email:
                type: string
              token:
                type: string
          examples:
            application/json:
              {
                "id_": "00000000-0000-0000-0000-000000000000",
                "email": "user@reana.info",
                "token": "Drmhze6EPcv0fN_81Bj-nA"
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
        existing_user = Session.query(User).filter_by(email=user_email).one_or_none()
        if existing_user:
            return jsonify({"message": "Email already exists."}), 400
        user_parameters = dict(api_key=secrets.token_urlsafe())
        user_parameters['email'] = user_email
        user = User(**user_parameters)
        Session.add(user)
        Session.commit()

        return jsonify({"message": "User was successfully created.",
                        "id_": user.id_,
                        "email": user.email,
                        "token": user.api_key}), 201

    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
