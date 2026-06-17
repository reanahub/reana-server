# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server group Flask-Blueprint."""

import logging

from flask import Blueprint, jsonify
from webargs import fields
from webargs.flaskparser import use_kwargs

from reana_server.decorators import signin_required
from reana_server.groups import get_group_backend, get_group_backends
from reana_server.groups.base import GroupBackendError

blueprint = Blueprint("groups", __name__)

MIN_SEARCH_LENGTH = 3
MAX_SEARCH_RESULTS = 20


@blueprint.route("/groups/search", methods=["GET"])
@signin_required()
@use_kwargs(
    {
        "query": fields.Str(required=True),
        "provider": fields.Str(),
    },
    location="query",
)
def search_groups(user, query, provider=None):
    r"""Search shareable groups by name.

    ---
    get:
      summary: Search shareable groups by name.
      description: >-
        This resource searches the configured group backends for groups
        matching the given query, for use in workflow sharing. Group
        member lists are never exposed.
      operationId: search_groups
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: API access_token of the user.
          required: false
          type: string
        - name: query
          in: query
          description: Required. Search string (at least 3 characters).
          required: true
          type: string
        - name: provider
          in: query
          description: Optional. Restrict the search to one group provider.
          required: false
          type: string
      responses:
        200:
          description: >-
            Request succeeded. The response contains matching groups.
          schema:
            type: object
            properties:
              items:
                type: array
                items:
                  type: object
                  properties:
                    provider:
                      type: string
                    external_id:
                      type: string
                    display_name:
                      type: string
          examples:
            application/json:
              {
                "items": [
                  {
                    "provider": "keycloak",
                    "external_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "display_name": "atlas",
                    "path": "/local/atlas"
                  }
                ]
              }
        400:
          description: >-
            Request failed. The query is too short or the provider unknown.
          schema:
            type: object
            properties:
              message:
                type: string
        401:
          description: >-
            Request failed. User not signed in.
          schema:
            type: object
            properties:
              message:
                type: string
        503:
          description: >-
            Request failed. The group backend is currently unavailable.
          schema:
            type: object
            properties:
              message:
                type: string
    """
    query = (query or "").strip()
    if len(query) < MIN_SEARCH_LENGTH:
        return (
            jsonify(
                message=(
                    f"Search query must be at least {MIN_SEARCH_LENGTH} "
                    "characters long."
                )
            ),
            400,
        )
    if provider:
        backend = get_group_backend(provider)
        if backend is None:
            return jsonify(message=f"Unknown group provider '{provider}'."), 400
        backends = {provider: backend}
    else:
        backends = get_group_backends()

    items = []
    try:
        for backend in backends.values():
            remaining = MAX_SEARCH_RESULTS - len(items)
            if remaining <= 0:
                break
            for ref in backend.search_groups(query, limit=remaining):
                items.append(
                    {
                        "provider": ref.provider,
                        "external_id": ref.external_id,
                        "display_name": ref.display_name,
                        "path": ref.path,
                    }
                )
    except GroupBackendError as error:
        logging.error("Group search failed: %s", error)
        return (
            jsonify(message="Group backend is currently unavailable."),
            503,
        )
    return jsonify(items=items), 200
