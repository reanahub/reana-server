# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Cluster capabilities endpoint (authenticated, role-optional)."""

from importlib.metadata import version

from fastapi import APIRouter, Security
from reana_commons.config import DEFAULT_WORKSPACE_PATH, WORKSPACE_PATHS
from reana_db.models import User

from reana_server.auth.deps import get_current_user
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
    REANA_KUBERNETES_JOBS_MIN_USER_UID,
    REANA_INTERACTIVE_SESSION_MAX_INACTIVITY_PERIOD,
    REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS,
    REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS_CUSTOM_ALLOWED,
    REANA_VETTED_CONTAINER_IMAGES,
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

router = APIRouter(tags=["info"])


@router.get("/info", summary="Cluster capabilities")
def info(user: User = Security(get_current_user, scopes=[])) -> dict:
    """Report cluster capabilities (compute backends, limits, engine versions).

    Role-optional like the legacy endpoint, so the UI can show capabilities to
    any authenticated user. Returns the structured dict directly.
    """
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
        kubernetes_min_user_uid=dict(
            title="Minimum allowed user runtime container UID for Kubernetes jobs",
            value=REANA_KUBERNETES_JOBS_MIN_USER_UID,
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
        vetted_container_images_enabled=dict(
            title="Vetted container images required for user workflows",
            value=REANA_VETTED_CONTAINER_IMAGES["enabled"],
        ),
        vetted_container_images_allowlist=dict(
            title="List of vetted container images allowed in user workflows",
            value=REANA_VETTED_CONTAINER_IMAGES["allowlist"],
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
            title="Snakemake engine version", value=version("snakemake")
        ),
        dask_enabled=dict(
            title="Dask workflows allowed in the cluster",
            value=bool(DASK_ENABLED),
        ),
        gitlab_host=dict(title="GitLab host", value=REANA_GITLAB_HOST),
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

    return cluster_information
