# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2020, 2021, 2025 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server quota functionality Flask-Blueprint."""
from typing import Optional

from flask import Blueprint, jsonify, request, Response
from marshmallow import fields, Schema
from reana_db.models import ResourceType

from reana_server.config import REANA_QUOTA_MANAGEMENT_SECRET
from reana_server.utils import _set_quota_limit, _get_users

blueprint = Blueprint("quota", __name__)


class SetQuotaLimitBodySchema(Schema):
    """Schema for set_quota_limit endpoint body."""

    user_id = fields.Str()
    email = fields.Str()
    resource_type = fields.Str(required=True)
    limit = fields.Int(required=True)


def _check_quota_management_secret() -> tuple[Response, int] | None:
    if not REANA_QUOTA_MANAGEMENT_SECRET:
        return jsonify(message="Quota functionality is not enabled"), 403

    # Check if secret is provided and matches the one in the config
    secret = request.headers.get("X-Quota-Management-Secret")
    if secret != REANA_QUOTA_MANAGEMENT_SECRET:
        return jsonify(message="Unauthorized"), 401

    return None


def _get_quota(
    resource_type: str,
    user_id: Optional[str] = None,
    email: Optional[str] = None,
    user_access_token: Optional[str] = None,
) -> tuple[int | None, int | None, str | None, int]:
    """
    Get quota limit and usage for a given user and resource type.

    :param resource_type: Type of the resource.
    :param user_id: ID of the user.
    :param email: Email of the user.
    :param user_access_token: Access token of the user.
    :return: Tuple with the limit, usage, the error message (or None if no error), and the status code.
    """
    users = _get_users(user_id, email, user_access_token) or None
    user = users[0] if users else None

    if not user:
        return None, None, "User not found.", 404

    if resource_type not in ResourceType._member_names_:
        return (
            None,
            None,
            f"Resource type '{resource_type}' is not one of the valid types: {', '.join(ResourceType._member_names_)}",
            400,
        )

    quota_usage = user.get_quota_usage()
    limit: int | None = quota_usage.get(resource_type, {}).get("limit", {}).get("raw")
    usage: int | None = quota_usage.get(resource_type, {}).get("usage", {}).get("raw")
    return limit, usage, None, 200


@blueprint.route("/quota", methods=["GET"])
def get_quota_usage():  # noqa
    r"""Endpoint to get quota limits.

    ---
    get:
      summary: Get resource quota limits.
      description: >-
        This endpoint gets resource quota limits for a given user.
      operationId: get_quota_usage
      produces:
        - application/json
      parameters:
        - name: X-Quota-Management-Secret
          in: header
          description: REANA user quota management secret
          required: true
          type: string
        - name: user_id
          in: query
          description: Get the quota limit by user ID (mutually exclusive with `email` and `user_access_token`)
          required: false
          type: string
        - name: email
          in: query
          description: Get the quota limit by user email (mutually exclusive with `user_id` and `user_access_token`)
          required: false
          type: string
        - name: user_access_token
          in: query
          description: Get the quota limit by user access token (mutually exclusive with `user_id` and `email`)
          required: false
          type: string
        - name: resource_type
          in: query
          description: The type of resource
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Raw resource quota limit is returned.
          schema:
            type: object
            properties:
              limit:
                type: number
              message:
                type: string
              usage:
                type: number
          examples:
            application/json:
              {
                "limit": 1000000000,
                "message": "OK",
                "usage": 500000000,
              }
        400:
          description: >-
            Request failed. The incoming data specification seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Resource type is required."
              }
            application/json:
              {
                "message": "No user specified."
              }
        401:
          description: >-
            Request failed. Unauthorized.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Unauthorized"
              }
        403:
          description: >-
            Request failed. Forbidden.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Quota functionality is not enabled."
              }
        404:
          description: >-
            Request failed. Not found.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Quota functionality is not enabled."
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
    response = _check_quota_management_secret()
    if response:
        return response

    # Get params from query string
    user_id = request.args.get("user_id")
    email = request.args.get("email")
    user_access_token = request.args.get("user_access_token")
    resource_type = request.args.get("resource_type")

    # Check if at least one of the user criteria is provided
    if not user_id and not email and not user_access_token:
        return jsonify(message="No user specified"), 400

    # Check if all user criteria are provided
    if int(bool(user_id)) + int(bool(email)) + int(bool(user_access_token)) > 1:
        return (
            jsonify(
                message="Exactly one of `user_id`, `email` or `user_access_token` must be provided.",
            ),
            400,
        )

    # Validate resource type
    if not resource_type:
        return jsonify(message="Resource type is required."), 400

    if resource_type not in ResourceType._member_names_:
        return (
            jsonify(
                message=f"Resource type '{resource_type}' does not exist. Available resource types are: {', '.join(ResourceType._member_names_)}"
            ),
            400,
        )

    limit, usage, error_msg, status = _get_quota(
        resource_type, user_id, email, user_access_token
    )
    if error_msg:
        return jsonify(message=error_msg), status

    if usage is None:
        return jsonify(message="Resource usage is not available."), 500

    if limit is None:
        return jsonify(limit=-1, usage=usage, message="Resource limit is not set."), 200

    return jsonify(limit=limit, usage=usage, message="OK"), 200


@blueprint.route("/quota", methods=["POST"])
def set_quota_limit():  # noqa
    r"""Endpoint to set quota limits.

    ---
    post:
      summary: Set resource quota limits.
      description: >-
        This endpoint sets resource quota limits for a given set of users.
      operationId: set_quota_limit
      produces:
        - application/json
      parameters:
        - name: X-Quota-Management-Secret
          in: header
          description: REANA user quota management secret
          required: true
          type: string
        - name: data
          in: body
          description: Data required to set quota limits (exactly one of `user_id` or `email` must be provided).
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
              resource_type:
                type: string
                description: Resource type to set
              limit:
                type: integer
                description: Raw quota limit to set
      responses:
        200:
          description: >-
            Resource quotas successfully set.
          schema:
            type: object
            properties:
              limit:
                type: number
              message:
                type: string
              usage:
                type: number
          examples:
            application/json:
              {
                "limit": 1000000000,
                "message": "OK",
                "usage": 500000000,
              }
        400:
          description: >-
            Request failed. The incoming data specification seems malformed.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Invalid request."
              }
        401:
          description: >-
            Request failed. Unauthorized.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Unauthorized"
              }
        403:
          description: >-
            Request failed. Forbidden.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Quota functionality is not enabled."
              }
        404:
          description: >-
            Request failed. User not found.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User not found."
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
                "message": "Quota could not be set: {error}"
              }
    """
    response = _check_quota_management_secret()
    if response:
        return response

    json_body: dict = request.get_json()
    errors = SetQuotaLimitBodySchema().validate(json_body)
    if errors:
        return jsonify(message=f"Invalid request. Errors: {errors}"), 400

    limit = json_body.get("limit")
    resource_type = json_body.get("resource_type")
    user_id = json_body.get("user_id")
    email = json_body.get("email")

    if int(bool(user_id)) + int(bool(email)) > 1:
        return (
            jsonify(
                message="Exactly one of `user_id` or `email` must be provided.",
            ),
            400,
        )

    msg, status_code, _ = _set_quota_limit(
        limit,
        resource_type=resource_type,
        user_ids=[user_id] if user_id else None,
        emails=[email] if email else None,
    )

    if status_code != 200:
        return jsonify(message=msg), status_code

    _, usage, error_msg, status_code = _get_quota(resource_type, user_id, email)
    if status_code != 200:
        return jsonify(message=error_msg), status_code

    return jsonify(limit=limit, usage=usage, message="OK"), status_code
