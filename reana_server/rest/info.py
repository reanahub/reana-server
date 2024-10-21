# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2021, 2022, 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server info functionality Flask-Blueprint."""

import logging
import traceback

from flask import Blueprint, jsonify
from marshmallow import Schema, fields

from reana_commons.config import DEFAULT_WORKSPACE_PATH, WORKSPACE_PATHS

from reana_server.config import (
    SUPPORTED_COMPUTE_BACKENDS,
    WORKSPACE_RETENTION_PERIOD,
    REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT,
    REANA_KUBERNETES_JOBS_MEMORY_LIMIT,
    REANA_KUBERNETES_JOBS_TIMEOUT_LIMIT,
    REANA_KUBERNETES_JOBS_MAX_USER_TIMEOUT_LIMIT,
    REANA_INTERACTIVE_SESSION_MAX_INACTIVITY_PERIOD,
    DASK_ENABLED,
    REANA_DASK_CLUSTER_MAX_CORES_LIMIT,
    REANA_DASK_CLUSTER_MAX_MEMORY_LIMIT,
    REANA_DASK_CLUSTER_DEFAULT_CORES_LIMIT,
    REANA_DASK_CLUSTER_DEFAULT_MEMORY_LIMIT,
    REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_CORES,
    REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_MEMORY,
)
from reana_server.decorators import signin_required

blueprint = Blueprint("info", __name__)


@blueprint.route("/info", methods=["GET"])
@signin_required(token_required=False)
def info(user, **kwargs):  # noqa
    r"""Get information about the cluster capabilities.

    ---
    get:
      summary: Get information about the cluster capabilities.
      operationId: info
      description: >-
        This resource reports information about cluster capabilities.
      produces:
       - application/json
      parameters:
        - name: access_token
          in: query
          description: The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. The response contains general info about the cluster.
          schema:
            properties:
              compute_backends:
                properties:
                  title:
                    type: string
                  value:
                    items:
                      type: string
                    type: array
                type: object
              default_kubernetes_jobs_timeout:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              default_kubernetes_memory_limit:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              default_workspace:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              kubernetes_max_memory_limit:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                    x-nullable: true
                type: object
              maximum_interactive_session_inactivity_period:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                    x-nullable: true
                type: object
              maximum_kubernetes_jobs_timeout:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              maximum_workspace_retention_period:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                    x-nullable: true
                type: object
              workspaces_available:
                properties:
                  title:
                    type: string
                  value:
                    items:
                      type: string
                    type: array
                type: object
              dask_enabled:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_cluster_default_cores_limit:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_cluster_max_cores_limit:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_cluster_default_memory_limit:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_cluster_max_memory_limit:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_cluster_default_single_worker_cores:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_cluster_default_single_worker_memory:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
            type: object
            required:
              - compute_backends
              - default_kubernetes_jobs_timeout
              - default_kubernetes_memory_limit
              - default_workspace
              - kubernetes_max_memory_limit
              - maximum_interactive_session_inactivity_period
              - maximum_kubernetes_jobs_timeout
              - maximum_workspace_retention_period
              - workspaces_available
              - dask_enabled
          examples:
            application/json:
              {
                "workspaces_available": {
                    "title": "List of available workspaces",
                    "value": ["/usr/share","/eos/home","/var/reana"]
                },
                "default_workspace": {
                    "title": "Default workspace",
                    "value": "/usr/share"
                },
                "compute_backends": {
                    "title": "List of supported compute backends",
                    "value": [
                        "kubernetes",
                        "htcondorcern",
                        "slurmcern"
                    ]
                },
                "kubernetes_memory_limit": {
                    "title": "Default memory limit for Kubernetes jobs",
                    "value": "3Gi"
                },
                "kubernetes_max_memory_limit": {
                    "title": "Maximum allowed memory limit for Kubernetes jobs",
                    "value": "10Gi"
                },
                "maximum_workspace_retention_period": {
                    "title": "Maximum retention period in days for workspace files",
                    "value": "3650"
                },
                "default_kubernetes_jobs_timeout": {
                    "title": "Default timeout for Kubernetes jobs",
                    "value": "604800"
                },
                "maximum_kubernetes_jobs_timeout": {
                    "title": "Maximum timeout for Kubernetes jobs",
                    "value": "1209600"
                },
                "dask_enabled": {
                    "title": "Dask workflows allowed in the cluster",
                    "value": "False"
                },
                "dask_cluster_default_cores_limit": {
                    "title": "Default cores limit for dask clusters",
                    "value": "4"
                },
                "dask_cluster_max_cores_limit": {
                    "title": "Maximum cores limit for dask clusters",
                    "value": "8"
                },
                "dask_cluster_default_memory_limit": {
                    "title": "Default memory limit for dask clusters",
                    "value": "2Gi"
                },
                "dask_cluster_max_memory_limit": {
                    "title": "Maximum memory limit for dask clusters",
                    "value": "4Gi"
                },
                "dask_cluster_default_single_worker_cores": {
                    "title": "Number of cores for one dask worker by default",
                    "value": "0.5"
                },
                "dask_cluster_default_single_worker_memory": {
                    "title": "Amount of memory for one dask worker by default",
                    "value": "256Mi"
                },
              }
        500:
          description: >-
            Request failed. Internal controller error.
          schema:
            type: object
            properties:
              message:
                type: string
          examples:
            application/json:
              {
                "message": "Internal controller error."
              }
    """
    try:
        cluster_information = dict(
            workspaces_available=dict(
                title="List of available workspaces",
                value=list(WORKSPACE_PATHS.values()),
            ),
            default_workspace=dict(
                title="Default workspace", value=DEFAULT_WORKSPACE_PATH
            ),
            compute_backends=dict(
                title="List of supported compute backends",
                value=SUPPORTED_COMPUTE_BACKENDS,
            ),
            default_kubernetes_memory_limit=dict(
                title="Default memory limit for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_MEMORY_LIMIT,
            ),
            kubernetes_max_memory_limit=dict(
                title="Maximum allowed memory limit for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT,
            ),
            maximum_workspace_retention_period=dict(
                title="Maximum retention period in days for workspace files",
                value=WORKSPACE_RETENTION_PERIOD,
            ),
            default_kubernetes_jobs_timeout=dict(
                title="Default timeout for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_TIMEOUT_LIMIT,
            ),
            maximum_kubernetes_jobs_timeout=dict(
                title="Maximum timeout for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_MAX_USER_TIMEOUT_LIMIT,
            ),
            maximum_interactive_session_inactivity_period=dict(
                title="Maximum inactivity period in days before automatic closure of interactive sessions",
                value=REANA_INTERACTIVE_SESSION_MAX_INACTIVITY_PERIOD,
            ),
            dask_enabled=dict(
                title="Dask workflows allowed in the cluster",
                value=bool(DASK_ENABLED),
            ),
        )
        if DASK_ENABLED:
            cluster_information["dask_cluster_default_cores_limit"] = dict(
                title="Default cores limit for dask clusters",
                value=REANA_DASK_CLUSTER_DEFAULT_CORES_LIMIT,
            )
            cluster_information["dask_cluster_max_cores_limit"] = dict(
                title="Maximum cores limit for dask clusters",
                value=REANA_DASK_CLUSTER_MAX_CORES_LIMIT,
            )
            cluster_information["dask_cluster_default_memory_limit"] = dict(
                title="Default memory limit for dask clusters",
                value=REANA_DASK_CLUSTER_DEFAULT_MEMORY_LIMIT,
            )
            cluster_information["dask_cluster_max_memory_limit"] = dict(
                title="Maximum memory limit for dask clusters",
                value=REANA_DASK_CLUSTER_MAX_MEMORY_LIMIT,
            )
            cluster_information["dask_cluster_default_single_worker_cores"] = dict(
                title="Number of cores for one dask worker by default",
                value=REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_CORES,
            )
            cluster_information["dask_cluster_default_single_worker_memory"] = dict(
                title="Memory for one dask worker by default",
                value=REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_MEMORY,
            )

        return InfoSchema().dump(cluster_information)

    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


class ListStringInfoValue(Schema):
    """Schema for a value represented by a list of strings."""

    title = fields.String()
    value = fields.List(fields.String())


class StringInfoValue(Schema):
    """Schema for a value represented by a string."""

    title = fields.String()
    value = fields.String(allow_none=False)


class StringNullableInfoValue(Schema):
    """Schema for a value represented by a nullable string."""

    title = fields.String()
    value = fields.String(allow_none=True)


class InfoSchema(Schema):
    """Marshmallow schema for ``info`` endpoint."""

    workspaces_available = fields.Nested(ListStringInfoValue)
    default_workspace = fields.Nested(StringInfoValue)
    compute_backends = fields.Nested(ListStringInfoValue)
    default_kubernetes_memory_limit = fields.Nested(StringInfoValue)
    kubernetes_max_memory_limit = fields.Nested(StringNullableInfoValue)
    maximum_workspace_retention_period = fields.Nested(StringNullableInfoValue)
    default_kubernetes_jobs_timeout = fields.Nested(StringInfoValue)
    maximum_kubernetes_jobs_timeout = fields.Nested(StringInfoValue)
    maximum_interactive_session_inactivity_period = fields.Nested(
        StringNullableInfoValue
    )
    kubernetes_max_memory_limit = fields.Nested(StringInfoValue)
    dask_enabled = fields.Nested(StringInfoValue)
    if DASK_ENABLED:
        dask_cluster_default_cores_limit = fields.Nested(StringInfoValue)
        dask_cluster_max_cores_limit = fields.Nested(StringInfoValue)
        dask_cluster_default_memory_limit = fields.Nested(StringInfoValue)
        dask_cluster_max_memory_limit = fields.Nested(StringInfoValue)
        dask_cluster_default_single_worker_cores = fields.Nested(StringInfoValue)
        dask_cluster_default_single_worker_memory = fields.Nested(StringInfoValue)
