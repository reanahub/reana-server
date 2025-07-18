# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2021, 2022, 2024, 2025 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server info functionality Flask-Blueprint."""

import logging
import traceback
from importlib.metadata import version

from flask import Blueprint, jsonify
from marshmallow import Schema, fields

from reana_commons.config import DEFAULT_WORKSPACE_PATH, WORKSPACE_PATHS

from reana_server.config import (
    SUPPORTED_COMPUTE_BACKENDS,
    WORKSPACE_RETENTION_PERIOD,
    REANA_KUBERNETES_JOBS_MAX_USER_CPU_REQUEST,
    REANA_KUBERNETES_JOBS_MAX_USER_CPU_LIMIT,
    REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_REQUEST,
    REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT,
    REANA_KUBERNETES_JOBS_CPU_REQUEST,
    REANA_KUBERNETES_JOBS_CPU_LIMIT,
    REANA_KUBERNETES_JOBS_MEMORY_REQUEST,
    REANA_KUBERNETES_JOBS_MEMORY_LIMIT,
    REANA_KUBERNETES_JOBS_TIMEOUT_LIMIT,
    REANA_KUBERNETES_JOBS_MAX_USER_TIMEOUT_LIMIT,
    REANA_INTERACTIVE_SESSION_MAX_INACTIVITY_PERIOD,
    REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS,
    REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS_CUSTOM_ALLOWED,
    DASK_ENABLED,
    DASK_AUTOSCALER_ENABLED,
    REANA_DASK_CLUSTER_DEFAULT_NUMBER_OF_WORKERS,
    REANA_DASK_CLUSTER_MAX_MEMORY_LIMIT,
    REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_MEMORY,
    REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_MEMORY,
    REANA_DASK_CLUSTER_MAX_NUMBER_OF_WORKERS,
    REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_THREADS,
    REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_THREADS,
    REANA_GITLAB_HOST,
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
              default_kubernetes_cpu_request:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                    x-nullable: true
                type: object
              default_kubernetes_cpu_limit:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                    x-nullable: true
                type: object
              default_kubernetes_memory_request:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                    x-nullable: true
                type: object
              default_kubernetes_memory_limit:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                    x-nullable: true
                type: object
              default_workspace:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              kubernetes_max_cpu_request:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                    x-nullable: true
                type: object
              kubernetes_max_cpu_limit:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                    x-nullable: true
                type: object
              kubernetes_max_memory_request:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                    x-nullable: true
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
              interactive_session_recommended_jupyter_images:
                properties:
                  title:
                    type: string
                  value:
                    type: array
                    items:
                      type: string
                type: object
              interactive_sessions_custom_image_allowed:
                properties:
                  title:
                    type: string
                  value:
                    type: string
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
              supported_workflow_engines:
                properties:
                  title:
                    type: string
                  value:
                    items:
                      type: string
                    type: array
                type: object
              cwl_engine_tool:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              cwl_engine_version:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              yadage_engine_version:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              yadage_engine_adage_version:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              yadage_engine_packtivity_version:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              snakemake_engine_version:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_enabled:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_autoscaler_enabled:
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
              dask_cluster_default_number_of_workers:
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
              dask_cluster_default_single_worker_threads:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_cluster_max_single_worker_memory:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_cluster_max_number_of_workers:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
              dask_cluster_max_single_worker_threads:
                properties:
                  title:
                    type: string
                  value:
                    type: string
                type: object
            type: object
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
                "kubernetes_cpu_request": {
                    "title": "Default CPU request for Kubernetes jobs",
                    "value": "1"
                },
                "kubernetes_cpu_limit": {
                    "title": "Default CPU limit for Kubernetes jobs",
                    "value": "2"
                },
                "kubernetes_memory_request": {
                    "title": "Default memory request for Kubernetes jobs",
                    "value": "1Gi"
                },
                "kubernetes_memory_limit": {
                    "title": "Default memory limit for Kubernetes jobs",
                    "value": "3Gi"
                },
                "kubernetes_max_cpu_request": {
                    "title": "Maximum allowed CPU request for Kubernetes jobs",
                    "value": "2"
                },
                "kubernetes_max_cpu_limit": {
                    "title": "Maximum allowed CPU limit for Kubernetes jobs",
                    "value": "4"
                },
                "kubernetes_max_memory_request": {
                    "title": "Maximum allowed memory request for Kubernetes jobs",
                    "value": "5Gi"
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
                "interactive_session_recommended_jupyter_images": {
                  "title": "Recommended Jupyter images for interactive sessions",
                  "value": [
                      'docker.io/jupyter/scipy-notebook:notebook-6.4.5',
                      'docker.io/jupyter/scipy-notebook:notebook-9.4.5',
                  ]
                },
                "interactive_sessions_custom_image_allowed": {
                    "title": "Whether users are allowed to spawn custom interactive session images",
                    "value": "False"
                },
                "supported_workflow_engines": {
                    "title": "List of supported workflow engines",
                    "value": [
                        'cwl',
                        'serial',
                        'snakemake',
                        'yadage'
                    ]
                },
                "cwl_engine_tool": {
                    "title": "CWL engine tool",
                    "value": "cwltool"
                },
                "cwl_engine_version": {
                    "title": "CWL engine version",
                    "value": "3.1.20210628163208"
                },
                "yadage_engine_version": {
                    "title": "Yadage engine version",
                    "value": "0.20.1"
                },
                "yadage_engine_adage_version": {
                    "title": "Yadage engine adage version",
                    "value": "0.11.0"
                },
                "yadage_engine_packtivity_version": {
                    "title": "Yadage engine packtivity version",
                    "value": "0.16.2"
                },
                "snakemake_engine_version": {
                    "title": "Snakemake engine version",
                    "value": "8.24.1"
                },
                "dask_enabled": {
                    "title": "Dask workflows allowed in the cluster",
                    "value": "False"
                },
                "gitlab_host": {
                    "title": "GitLab host",
                    "value": "gitlab.cern.ch"
                },
                "dask_autoscaler_enabled": {
                    "title": "Dask autoscaler enabled in the cluster",
                    "value": "False"
                },
                "dask_cluster_max_memory_limit": {
                    "title": "The maximum memory limit for Dask clusters created by users",
                    "value": "16Gi"
                },
                "dask_cluster_default_number_of_workers": {
                    "title": "The number of Dask workers created by default",
                    "value": "2Gi"
                },
                "dask_cluster_default_single_worker_memory": {
                    "title": "The amount of memory used by default by a single Dask worker",
                    "value": "2Gi"
                },
                "dask_cluster_default_single_worker_threads": {
                    "title": "The number of threads used by default by a single Dask worker",
                    "value": "4"
                },
                "dask_cluster_max_single_worker_memory": {
                    "title": "The maximum amount of memory that users can ask for the single Dask worker",
                    "value": "8Gi"
                },
                "dask_cluster_max_number_of_workers": {
                    "title": "The maximum number of workers that users can ask for the single Dask cluster",
                    "value": "20"
                },
                "dask_cluster_max_single_worker_threads": {
                    "title": "The maximum number of threads that users can ask for the single Dask worker",
                    "value": "8"
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
            default_kubernetes_cpu_request=dict(
                title="Default CPU request for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_CPU_REQUEST,
            ),
            default_kubernetes_cpu_limit=dict(
                title="Default CPU limit for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_CPU_LIMIT,
            ),
            default_kubernetes_memory_request=dict(
                title="Default memory request for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_MEMORY_REQUEST,
            ),
            default_kubernetes_memory_limit=dict(
                title="Default memory limit for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_MEMORY_LIMIT,
            ),
            kubernetes_max_cpu_request=dict(
                title="Maximum allowed CPU request for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_MAX_USER_CPU_REQUEST,
            ),
            kubernetes_max_cpu_limit=dict(
                title="Maximum allowed CPU limit for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_MAX_USER_CPU_LIMIT,
            ),
            kubernetes_max_memory_request=dict(
                title="Maximum allowed memory request for Kubernetes jobs",
                value=REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_REQUEST,
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
            interactive_sessions_custom_image_allowed=dict(
                title="Users can set custom interactive session images",
                value=REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS_CUSTOM_ALLOWED,
            ),
            interactive_session_recommended_jupyter_images=dict(
                title="Recommended Jupyter images for interactive sessions",
                value=[
                    item["image"]
                    for item in REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS["jupyter"][
                        "recommended"
                    ]
                ],
            ),
            supported_workflow_engines=dict(
                title="List of supported workflow engines",
                value=["cwl", "serial", "snakemake", "yadage"],
            ),
            cwl_engine_tool=dict(title="CWL engine tool", value="cwltool"),
            cwl_engine_version=dict(
                title="CWL engine version", value=version("cwltool")
            ),
            yadage_engine_version=dict(
                title="Yadage engine version", value=version("yadage")
            ),
            yadage_engine_adage_version=dict(
                title="Yadage engine adage version", value=version("adage")
            ),
            yadage_engine_packtivity_version=dict(
                title="Yadage engine packtivity version", value=version("packtivity")
            ),
            snakemake_engine_version=dict(
                title="Snakemake engine version",
                value=version("snakemake"),
            ),
            dask_enabled=dict(
                title="Dask workflows allowed in the cluster",
                value=bool(DASK_ENABLED),
            ),
            gitlab_host=dict(
                title="GitLab host",
                value=REANA_GITLAB_HOST,
            ),
        )

        if DASK_ENABLED:
            cluster_information["dask_autoscaler_enabled"] = dict(
                title="Dask autoscaler enabled in the cluster",
                value=bool(DASK_AUTOSCALER_ENABLED),
            )
            cluster_information["dask_cluster_default_number_of_workers"] = dict(
                title="The number of Dask workers created by default",
                value=REANA_DASK_CLUSTER_DEFAULT_NUMBER_OF_WORKERS,
            )
            cluster_information["dask_cluster_max_memory_limit"] = dict(
                title="The maximum memory limit for Dask clusters created by users",
                value=REANA_DASK_CLUSTER_MAX_MEMORY_LIMIT,
            )
            cluster_information["dask_cluster_default_single_worker_memory"] = dict(
                title="The amount of memory used by default by a single Dask worker",
                value=REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_MEMORY,
            )
            cluster_information["dask_cluster_max_single_worker_memory"] = dict(
                title="The maximum amount of memory that users can ask for the single Dask worker",
                value=REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_MEMORY,
            )
            cluster_information["dask_cluster_max_number_of_workers"] = dict(
                title="The maximum number of workers that users can ask for the single Dask cluster",
                value=REANA_DASK_CLUSTER_MAX_NUMBER_OF_WORKERS,
            )
            cluster_information["dask_cluster_default_single_worker_threads"] = dict(
                title="The number of threads used by default by a single Dask worker",
                value=REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_THREADS,
            )
            cluster_information["dask_cluster_max_single_worker_threads"] = dict(
                title="The maximum number of threads that users can ask for the single Dask worker",
                value=REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_THREADS,
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
    default_kubernetes_cpu_request = fields.Nested(StringNullableInfoValue)
    default_kubernetes_cpu_limit = fields.Nested(StringNullableInfoValue)
    default_kubernetes_memory_request = fields.Nested(StringNullableInfoValue)
    default_kubernetes_memory_limit = fields.Nested(StringInfoValue)
    kubernetes_max_cpu_request = fields.Nested(StringNullableInfoValue)
    kubernetes_max_cpu_limit = fields.Nested(StringNullableInfoValue)
    kubernetes_max_memory_request = fields.Nested(StringNullableInfoValue)
    kubernetes_max_memory_limit = fields.Nested(StringNullableInfoValue)
    maximum_workspace_retention_period = fields.Nested(StringNullableInfoValue)
    default_kubernetes_jobs_timeout = fields.Nested(StringInfoValue)
    maximum_kubernetes_jobs_timeout = fields.Nested(StringInfoValue)
    maximum_interactive_session_inactivity_period = fields.Nested(
        StringNullableInfoValue
    )
    kubernetes_max_memory_limit = fields.Nested(StringInfoValue)
    interactive_session_recommended_jupyter_images = fields.Nested(ListStringInfoValue)
    interactive_sessions_custom_image_allowed = fields.Nested(StringInfoValue)
    supported_workflow_engines = fields.Nested(ListStringInfoValue)
    cwl_engine_tool = fields.Nested(StringInfoValue)
    cwl_engine_version = fields.Nested(StringInfoValue)
    yadage_engine_version = fields.Nested(StringInfoValue)
    yadage_engine_adage_version = fields.Nested(StringInfoValue)
    yadage_engine_packtivity_version = fields.Nested(StringInfoValue)
    snakemake_engine_version = fields.Nested(StringInfoValue)
    dask_enabled = fields.Nested(StringInfoValue)
    gitlab_host = fields.Nested(StringInfoValue)

    if DASK_ENABLED:
        dask_autoscaler_enabled = fields.Nested(StringInfoValue)
        dask_cluster_default_number_of_workers = fields.Nested(StringInfoValue)
        dask_cluster_max_memory_limit = fields.Nested(StringInfoValue)
        dask_cluster_default_single_worker_memory = fields.Nested(StringInfoValue)
        dask_cluster_max_single_worker_memory = fields.Nested(StringInfoValue)
        dask_cluster_max_number_of_workers = fields.Nested(StringInfoValue)
        dask_cluster_default_single_worker_threads = fields.Nested(StringInfoValue)
        dask_cluster_max_single_worker_threads = fields.Nested(StringInfoValue)
