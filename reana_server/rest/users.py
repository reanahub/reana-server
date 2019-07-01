# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server User Endpoints."""

import logging
import json
import traceback

from flask import Blueprint, jsonify, request
from flask_jwt_extended import (
    create_access_token, create_refresh_token,
    set_access_cookies, set_refresh_cookies
)
from reana_server.utils import _create_user, _get_users

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


@blueprint.route('/login', methods=['GET'])
def user_login():
    r"""Endpoint to authenticate users.

    ---
    get:
      summary: TEMPORARY displays a message about how to access a resource.
      description: >-
        This resource provides guidance to the user on how to access a file.
        It tells the user to add the access token as an URL param.
      operationId: user_login
      produces:
        - application/json
      responses:
        200:
          description: >-
            Message is displayed on the browser.
          schema:
              type: object
              properties:
                message:
                  type: string
          examples:
            application/json:
              {
                "message": "Add your REANA_ACCESS_TOKEN as a param."
              }
    """
    return jsonify({"message": "Add your REANA_ACCESS_TOKEN as a URL param " +
                    "to access the resource. " +
                    "RESOURCE_URL?access_token=REANA_ACCESS_TOKEN"}), 200


@blueprint.route('/auth', methods=['POST'])
def auth_user():
    r"""Endpoint to authenticate users.

    ---
    post:
      summary: Authenticates the user with the provided information.
      description: >-
        This resource looks for an user with the provided
        information (email, id) and, if there is such user, creates
        a JWT cookie on the user browser.
      operationId: auth_user
      produces:
        - application/json
      parameters:
        - name: email
          in: query
          description: Required. The email of the user.
          required: true
          type: string
        - name: password
          in: query
          description: Required. API key of the admin.
          required: true
          type: string
      responses:
        200:
          description: >-
            User authenticate successfully. Returns the JWT set cookie
            headers and a successful login boolean.
          schema:
            type: object
            properties:
              login:
                type: boolean
          examples:
            application/json:
              {
                "login": true
              }
        401:
          description: >-
            Request failed. The data provided could authenticate
            the user.
          schema:
            type: object
            properties:
              login:
                type: boolean
          examples:
            application/json:
              {
                "message": "Couldn't authenticate."
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
        data = json.loads(request.data.decode('utf-8'))
        user_email = data['username']
        reana_access_token = data['password']
        users = _get_users(None, user_email, None, reana_access_token)
        if users:
            user = users[0]
            access_token = create_access_token(identity=user.id_)
            refresh_token = create_refresh_token(identity=user.id_)

            response = jsonify({'login': True})
            set_access_cookies(response, access_token)
            set_refresh_cookies(response, refresh_token)
            return response, 200
        else:
            response = jsonify({'message': "Could not authenticate user."})
            return response, 401
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
