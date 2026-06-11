# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""User notification REST API."""

from datetime import datetime

from flask import Blueprint, jsonify
from reana_db.database import Session
from reana_db.models import Notification
from reana_server.decorators import signin_required
from webargs import fields, validate
from webargs.flaskparser import use_kwargs

blueprint = Blueprint("notifications", __name__)


def _serialize_notification(notification):
    """Serialize a notification for the API."""
    return {
        "id": str(notification.id_),
        "type": notification.type_,
        "payload": notification.payload,
        "read_at": notification.read_at.isoformat() if notification.read_at else None,
        "created": notification.created.isoformat() if notification.created else None,
    }


@blueprint.route("/notifications", methods=["GET"])
@use_kwargs(
    {"limit": fields.Int(load_default=20, validate=validate.Range(min=1, max=100))},
    location="query",
)
@signin_required(token_required=False)
def get_notifications(user, limit):
    r"""Return the authenticated user's latest notifications.

    ---
    get:
      summary: Get user notifications.
      description: >-
        Returns the authenticated user's latest notifications, ordered from
        newest to oldest, together with the total number of unread
        notifications.
      operationId: get_notifications
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: API access token of the notification recipient.
          required: false
          type: string
        - name: limit
          in: query
          description: Maximum number of notifications to return.
          required: false
          type: integer
          default: 20
          minimum: 1
          maximum: 100
      responses:
        200:
          description: Notifications successfully retrieved.
          schema:
            type: object
            properties:
              notifications:
                type: array
                items:
                  type: object
                  properties:
                    id:
                      type: string
                    type:
                      type: string
                    payload:
                      type: object
                      additionalProperties: true
                    read_at:
                      type: string
                      format: date-time
                      x-nullable: true
                    created:
                      type: string
                      format: date-time
                      x-nullable: true
              unread_count:
                type: integer
          examples:
            application/json:
              {
                "notifications": [
                  {
                    "id": "d34c6438-6817-4f33-a09e-83a17ae17851",
                    "type": "workflow_shared",
                    "payload": {
                      "workflow_id": "cdcf48b1-c2f3-4693-8230-b066e088c6ac",
                      "workflow_name": "my-analysis.1",
                      "sharer_email": "alice@example.org",
                      "message": "Please review this workflow.",
                      "valid_until": null
                    },
                    "read_at": null,
                    "created": "2026-06-09T10:00:00"
                  }
                ],
                "unread_count": 1
              }
        400:
          description: Request failed. Query parameters are malformed.
          schema:
            type: object
            properties:
              message:
                type: string
        401:
          description: Request failed. User is not signed in.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User not signed in"
              }
    """
    query = Session.query(Notification).filter_by(user_id=user.id_)
    unread_count = query.filter(Notification.read_at.is_(None)).count()
    notifications = query.order_by(Notification.created.desc()).limit(limit).all()
    return jsonify(
        notifications=[_serialize_notification(item) for item in notifications],
        unread_count=unread_count,
    )


@blueprint.route("/notifications/<notification_id>", methods=["PATCH"])
@use_kwargs({"read": fields.Bool(required=True)}, location="json")
@signin_required(token_required=False)
def update_notification(notification_id, user, read):
    r"""Update one of the authenticated user's notifications.

    ---
    patch:
      summary: Update a user notification.
      description: >-
        Marks one of the authenticated user's notifications as read or unread.
        Notifications belonging to another user are not accessible.
      operationId: update_notification
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: API access token of the notification recipient.
          required: false
          type: string
        - name: notification_id
          in: path
          description: Notification UUID.
          required: true
          type: string
        - name: notification_update
          in: body
          description: Notification fields to update.
          required: true
          schema:
            type: object
            properties:
              read:
                type: boolean
                description: Whether the notification should be marked as read.
            required:
              - read
      responses:
        200:
          description: Notification successfully updated.
          schema:
            type: object
            properties:
              notification:
                type: object
                properties:
                  id:
                    type: string
                  type:
                    type: string
                  payload:
                    type: object
                    additionalProperties: true
                  read_at:
                    type: string
                    format: date-time
                    x-nullable: true
                  created:
                    type: string
                    format: date-time
                    x-nullable: true
        400:
          description: Request failed. The request body is malformed.
          schema:
            type: object
            properties:
              message:
                type: string
        401:
          description: Request failed. User is not signed in.
          schema:
            type: object
            properties:
              message:
                type: string
        404:
          description: Request failed. Notification does not exist.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Notification not found."
              }
    """
    notification = (
        Session.query(Notification)
        .filter_by(id_=notification_id, user_id=user.id_)
        .one_or_none()
    )
    if not notification:
        return jsonify(message="Notification not found."), 404

    notification.read_at = datetime.utcnow() if read else None
    Session.commit()
    return jsonify(notification=_serialize_notification(notification))


@blueprint.route("/notifications/read-all", methods=["POST"])
@signin_required(token_required=False)
def read_all_notifications(user):
    r"""Mark all of the authenticated user's notifications as read.

    ---
    post:
      summary: Mark all user notifications as read.
      description: >-
        Marks every unread notification belonging to the authenticated user
        as read.
      operationId: read_all_notifications
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: API access token of the notification recipient.
          required: false
          type: string
      responses:
        200:
          description: Notifications successfully marked as read.
          schema:
            type: object
            properties:
              updated:
                type: integer
                description: Number of notifications marked as read.
          examples:
            application/json:
              {
                "updated": 3
              }
        401:
          description: Request failed. User is not signed in.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "User not signed in"
              }
    """
    read_at = datetime.utcnow()
    updated = (
        Session.query(Notification)
        .filter_by(user_id=user.id_, read_at=None)
        .update({Notification.read_at: read_at}, synchronize_session=False)
    )
    Session.commit()
    return jsonify(updated=updated)
