# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server User Endpoints."""

import json
import logging
import traceback

from flask import Blueprint, jsonify, request
from reana_commons.errors import REANASecretAlreadyExists, REANASecretDoesNotExist
from reana_commons.k8s.secrets import UserSecretsStore, Secret
from webargs import fields
from webargs.flaskparser import use_kwargs
from marshmallow import Schema, validate

from reana_server.decorators import signin_required


blueprint = Blueprint("secrets", __name__)


class AddSecretsBodySchema(Schema):
    """Schema for add_secrets endpoint body."""

    body = (
        fields.Dict(
            keys=fields.Str(),
            values=fields.Nested(
                {
                    "value": fields.Str(required=True),
                    "type": fields.Str(
                        validate=validate.OneOf(Secret.types), required=True
                    ),
                }
            ),
            required=True,
        ),
    )


@blueprint.route("/secrets/", methods=["POST"])
@signin_required()
@use_kwargs(
    {
        "overwrite": fields.Bool(missing=False, location="query"),
    }
)
def add_secrets(user, overwrite=False):
    r"""Endpoint to create user secrets.

    ---
    post:
      summary: Add user secrets to REANA.
      description: >-
        This resource adds secrets for the authenticated user.
      operationId: add_secrets
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: Secrets owner access token.
          required: false
          type: string
        - name: overwrite
          in: query
          description: Whether existing secret keys should be overwritten.
          required: false
          type: boolean
        - name: secrets
          in: body
          description: >-
            Optional. List of secrets to be added.
          required: true
          schema:
            type: object
            additionalProperties:
              type: object
              description: Secret definition.
              properties:
                value:
                  type: string
                  description: Secret value
                type:
                  type: string
                  enum:
                    - env
                    - file
                  description: >-
                    How will be the secret assigned to the jobs, either
                    exported as an environment variable or mounted as a file.
      responses:
        201:
          description: >-
            Secrets successfully added.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Secret(s) successfully added."
              }
        403:
          description: >-
            Request failed. Token is not valid.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Token is not valid"
              }
        409:
          description: >-
            Request failed. Secrets could not be added due to a conflict.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "The submitted secrets api_key, password,
                            username already exist."
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
    json_body = request.json
    AddSecretsBodySchema(strict=True).validate({"body": json_body})

    try:
        secrets = [
            Secret.from_base64(
                name=secret_name,
                value=secret["value"],
                type_=secret["type"],
            )
            for secret_name, secret in json_body.items()
        ]
    except ValueError as e:
        # value is not correctly base64-encoded
        return jsonify({"message": str(e)}), 400

    try:
        user_secrets = UserSecretsStore.fetch(user.id_)
        user_secrets.add_secrets(secrets, overwrite=overwrite)
        UserSecretsStore.update(user_secrets)
        return jsonify({"message": "Secret(s) successfully added."}), 201
    except REANASecretAlreadyExists as e:
        return jsonify({"message": str(e)}), 409
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route("/secrets", methods=["GET"])
@signin_required()
def get_secrets(user):  # noqa
    r"""Endpoint to retrieve user secrets.

    ---
    get:
      summary: Get user secrets. Requires an user access token.
      description: >-
        Get user secrets.
      operationId: get_secrets
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: Secrets owner access token.
          required: false
          type: string
      responses:
        200:
          description: >-
            List of user secrets.
          schema:
            type: array
            items:
              properties:
                name:
                  type: string
                  description: Secret name
                type:
                  type: string
                  enum:
                    - env
                    - file
                  description: >-
                    How will be the secret assigned to
                    the jobs, either exported as an environment
                    variable or mounted as a file.
          examples:
            application/json:
              [
                {
                  "name": ".keytab",
                  "value": "SGVsbG8gUkVBTkEh",
                },
                {
                  "name": "username",
                  "value": "reanauser",
                },
              ]
        403:
          description: >-
            Request failed. Token is not valid.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Token is not valid"
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
                "message": "Error while querying."
              }
    """
    try:
        user_secrets = UserSecretsStore.fetch(user.id_)
        user_secrets_json = [
            {"name": secret.name, "type": secret.type_}
            for secret in user_secrets.get_secrets()
        ]
        return jsonify(user_secrets_json), 200
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


class DeleteSecretsBodySchema(Schema):
    """Schema for delete_secrets endpoint body."""

    body = fields.List(fields.Str(), required=True)


@blueprint.route("/secrets/", methods=["DELETE"])
@signin_required()
def delete_secrets(user):  # noqa
    r"""Endpoint to delete user secrets.

    ---
    delete:
      summary: Deletes the specified secret(s).
      description: >-
        This resource deletes the requested secrets.
      operationId: delete_secrets
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: API key of the admin.
          required: false
          type: string
        - name: secrets
          in: body
          description: >-
            Optional. List of secrets to be deleted.
          required: true
          schema:
            type: array
            description: List of secret names to be deleted.
            items:
              type: string
              description: Secret name to be deleted.
      responses:
        200:
          description: >-
            Secrets successfully deleted.
          schema:
            type: array
            description: List of secret names that have been deleted.
            items:
              type: string
              description: Name of the secret that have been deleted.
          examples:
            application/json:
              [
                ".keytab",
                "username",
              ]
        403:
          description: >-
            Request failed. Token is not valid.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Token is not valid"
              }
        404:
          description: >-
            Request failed. Secrets do not exist.
          schema:
            type: array
            description: List of secret names that could not be deleted.
            items:
              type: string
              description: Name of the secret which does not exist.
          examples:
            application/json:
              [
                "certificate.pem",
                "PASSWORD",
              ]
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
    json_body = request.json
    DeleteSecretsBodySchema(strict=True).validate({"body": json_body})
    secrets = json_body

    try:
        user_secrets = UserSecretsStore.fetch(user.id_)
        deleted_secrets_list = user_secrets.delete_secrets(secrets)
        UserSecretsStore.update(user_secrets)
        return jsonify(deleted_secrets_list), 200
    except REANASecretDoesNotExist as e:
        return jsonify(e.missing_secrets_list), 404
    except ValueError:
        return jsonify({"message": "Token is not valid."}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
