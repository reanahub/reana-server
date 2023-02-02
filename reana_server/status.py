# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020, 2021, 2022, 2023 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Status module for REANA."""

import enum
import logging
import subprocess
from datetime import datetime, timedelta

from invenio_accounts.models import SessionActivity
from kubernetes.client.rest import ApiException
from marshmallow import Schema, fields
from reana_commons.config import (
    REANA_COMPONENT_PREFIX,
    REANA_COMPUTE_BACKENDS,
    REANA_INFRASTRUCTURE_KUBERNETES_NAMESPACE,
    REANA_RUNTIME_KUBERNETES_NAMESPACE,
    REANA_MAX_CONCURRENT_BATCH_WORKFLOWS,
    SHARED_VOLUME_PATH,
)
from reana_commons.job_utils import kubernetes_memory_to_bytes
from reana_commons.k8s.api_client import (
    current_k8s_corev1_api_client,
    current_k8s_custom_objects_api_client,
)
from reana_commons.utils import get_usage_percentage
from reana_db.database import Session
from reana_db.models import (
    InteractiveSession,
    Job,
    JobStatus,
    Resource,
    ResourceType,
    ResourceUnit,
    RunStatus,
    UserResource,
    Workflow,
)
from sqlalchemy import desc

from reana_server.config import REANA_KUBERNETES_JOBS_MEMORY_LIMIT_IN_BYTES


class REANAStatus:
    """REANA Status interface."""

    def __init__(self, from_=None, until=None, user=None):
        """Initialise REANAStatus class."""
        self.from_ = from_ or (datetime.now() - timedelta(days=1))
        self.until = until or datetime.now()
        self.user = user
        self.namespaces = {
            REANA_INFRASTRUCTURE_KUBERNETES_NAMESPACE,
            REANA_RUNTIME_KUBERNETES_NAMESPACE,
        }

    def execute_cmd(self, cmd):
        """Execute a command."""
        return subprocess.check_output(cmd).decode().rstrip("\r\n")

    def get_status(self):
        """Get status summary for REANA."""
        raise NotImplementedError()


class InteractiveSessionsStatus(REANAStatus):
    """Class to retrieve statistics related to REANA interactive sessions."""

    def __init__(self, from_=None, until=None, user=None):
        """Initialise InteractiveSessionsStatus class.

        :param from_: From which moment in time to collect information. Not
            implemented yet.
        :param until: Until which moment in time to collect information. Not
            implemented yet.
        :param user: A REANA-DB user model.
        :type from_: datetime
        :type until: datetime
        :type user: reana_db.models.User
        """
        super().__init__(from_=from_, until=until, user=user)

    def get_active(self):
        """Get the number of active interactive sessions."""
        non_active_statuses = [
            RunStatus.stopped,
            RunStatus.deleted,
            RunStatus.failed,
        ]
        active_interactive_sessions = (
            Session.query(InteractiveSession)
            .filter(InteractiveSession.status.notin_(non_active_statuses))
            .count()
        )
        return active_interactive_sessions

    def get_status(self):
        """Get status summary for interactive sessions."""
        return {
            "active": self.get_active(),
        }


class SystemStatus(REANAStatus):
    """Class to retrieve statistics related to the current REANA component."""

    def __init__(self, from_=None, until=None, user=None):
        """Initialise SystemStatus class.

        :param from_: From which moment in time to collect information. Not
            implemented yet.
        :param until: Until which moment in time to collect information. Not
            implemented yet.
        :param user: A REANA-DB user model.
        :type from_: datetime
        :type until: datetime
        :type user: reana_db.models.User
        """
        super().__init__(from_=from_, until=until, user=user)

    def uptime(self):
        """Get component uptime."""
        cmd = ["uptime", "-p"]
        return self.execute_cmd(cmd)

    def get_status(self):
        """Get status summary for REANA system."""
        return {
            "uptime": self.uptime(),
        }


class StorageStatus(REANAStatus):
    """Class to retrieve statistics related to REANA storage."""

    def __init__(self, from_=None, until=None, user=None):
        """Initialise StorageStatus class.

        :param from_: From which moment in time to collect information. Not
            implemented yet.
        :param until: Until which moment in time to collect information. Not
            implemented yet.
        :param user: A REANA-DB user model.
        :type from_: datetime
        :type until: datetime
        :type user: reana_db.models.User
        """
        super().__init__(from_=from_, until=until, user=user)

    def shared_volume_health(self):
        """REANA shared volume health."""
        cmd = ["df", "-h", "--output=used,size,pcent", SHARED_VOLUME_PATH]
        output = self.execute_cmd(cmd).splitlines()
        used_size, total_size, used_percentage = output[1].split()

        return f"{used_size}/{total_size} ({used_percentage})"

    def get_status(self):
        """Get status summary for REANA storage."""
        return {
            "shared_volume_health": self.shared_volume_health(),
        }


class UsersStatus(REANAStatus):
    """Class to retrieve statistics related to REANA users."""

    def __init__(self, from_=None, until=None, user=None):
        """Initialise UsersStatus class.

        :param from_: From which moment in time to collect information. Not
            implemented yet.
        :param until: Until which moment in time to collect information. Not
            implemented yet.
        :param user: A REANA-DB user model.
        :type from_: datetime
        :type until: datetime
        :type user: reana_db.models.User
        """
        super().__init__(from_=from_, until=until, user=user)

    def active_web_users(self):
        """Get the number of active web users.

        Depends on how long does a session last.
        """
        return Session.query(SessionActivity).count()

    def get_status(self):
        """Get status summary for REANA users."""
        return {
            "active_web_users": self.active_web_users(),
        }


class WorkflowsStatus(REANAStatus):
    """Class to retrieve statistics related to REANA workflows."""

    def __init__(self, from_=None, until=None, user=None):
        """Initialise WorkflowsStatus class.

        :param from_: From which moment in time to collect information. Not
            implemented yet.
        :param until: Until which moment in time to collect information. Not
            implemented yet.
        :param user: A REANA-DB user model.
        :type from_: datetime
        :type until: datetime
        :type user: reana_db.models.User
        """
        super().__init__(from_=from_, until=until, user=user)

    def get_workflows_by_status(self, status):
        """Get the number of workflows in status ``status``."""
        number = Session.query(Workflow).filter(Workflow.status == status).count()

        return number

    def restarted_workflows(self):
        """Get the number of restarted workflows."""
        number = Session.query(Workflow).filter(Workflow.restart).count()

        return number

    def stuck_in_running_workflows(self):
        """Get the number of stuck running workflows."""
        inactivity_threshold = datetime.now() - timedelta(hours=12)
        number = (
            Session.query(Workflow)
            .filter(Workflow.status == RunStatus.running)
            .filter(Workflow.run_started_at <= inactivity_threshold)
            .filter(Workflow.updated <= inactivity_threshold)
            .count()
        )

        return number

    def stuck_in_pending_workflows(self):
        """Get the number of stuck pending workflows."""
        inactivity_threshold = datetime.now() - timedelta(minutes=20)
        number = (
            Session.query(Workflow)
            .filter(Workflow.status == RunStatus.pending)
            .filter(Workflow.updated <= inactivity_threshold)
            .count()
        )

        return number

    def git_workflows(self):
        """Get the number of Git based workflows."""
        number = Session.query(Workflow).filter(Workflow.git_repo != "").count()

        return number

    def get_status(self):
        """Get status summary for REANA workflows."""
        return {
            "running": self.get_workflows_by_status(RunStatus.running),
            "finished": self.get_workflows_by_status(RunStatus.finished),
            "stuck in running": self.stuck_in_running_workflows(),
            "stuck in pending": self.stuck_in_pending_workflows(),
            "queued": self.get_workflows_by_status(RunStatus.queued),
            "restarts": self.restarted_workflows(),
            "git_source": self.git_workflows(),
        }


class QuotaUsageStatus(REANAStatus):
    """Class to retrieve statistics related to the current REANA users quota usage."""

    def __init__(self, from_=None, until=None, user=None):
        """Initialise QuotaUsageStatus class.

        :param from_: From which moment in time to collect information. Not
            implemented yet.
        :param until: Until which moment in time to collect information. Not
            implemented yet.
        :param user: A REANA-DB user model.
        :type from_: datetime
        :type until: datetime
        :type user: reana_db.models.User
        """
        super().__init__(from_=from_, until=until, user=user)

    def format_user_data(self, users):
        """Format user data with human readable units."""
        return [
            {
                "email": user.user.email,
                "used": ResourceUnit.human_readable_unit(
                    user.resource.unit, user.quota_used
                ),
                "limit": ResourceUnit.human_readable_unit(
                    user.resource.unit, user.quota_limit
                ),
                "percentage": get_usage_percentage(user.quota_used, user.quota_limit),
            }
            for user in users
        ]

    def get_top_five_percentage(self, resource_type):
        """Get the top five users with highest quota usage percentage."""
        users = (
            Session.query(UserResource)
            .join(UserResource.resource)
            .filter(Resource.type_ == resource_type)
            .filter(UserResource.quota_limit != 0)
            .order_by(desc(UserResource.quota_used * 100.0 / UserResource.quota_limit))
            .limit(5)
        )
        return self.format_user_data(users)

    def get_top_five(self, resource_type):
        """Get the top five users according to quota usage."""
        users = (
            Session.query(UserResource)
            .join(UserResource.resource)
            .filter(Resource.type_ == resource_type)
            .order_by(UserResource.quota_used.desc())
            .limit(5)
        )
        return self.format_user_data(users)

    def get_status(self):
        """Get status summary for REANA quota usage."""
        return {
            "top_five_disk": self.get_top_five(ResourceType.disk),
            "top_five_cpu": self.get_top_five(ResourceType.cpu),
            "top_five_disk_percentage": self.get_top_five_percentage(ResourceType.disk),
            "top_five_cpu_percentage": self.get_top_five_percentage(ResourceType.cpu),
        }


class NodesStatus(REANAStatus):
    """Class to retrieve statistics related to REANA cluster nodes."""

    def get_nodes(self):
        """Get list of all node names."""
        nodes = current_k8s_corev1_api_client.list_node()
        return [node.metadata.name for node in nodes.items]

    def get_unschedulable_nodes(self):
        """Get list of node names that are not schedulable."""
        nodes = current_k8s_corev1_api_client.list_node(
            field_selector="spec.unschedulable=true"
        )
        return [node.metadata.name for node in nodes.items]

    def get_nodes_memory(self):
        """Get list of all node memory capacities."""
        try:
            nodes = current_k8s_corev1_api_client.list_node()
            return [
                kubernetes_memory_to_bytes(node.status.capacity["memory"])
                for node in nodes.items
            ]
        except ValueError as e:
            # FIXME: after new Kubernetes release this should be not needed
            msg = "Error while retreiving k8s list of nodes."
            logging.error(msg)
            logging.error(e)
            return []

    def get_total_memory(self):
        """Get total memory from all nodes."""
        return sum(self.get_nodes_memory())

    def get_memory_usage(self):
        """Get nodes memory usage."""
        result = dict()
        try:
            nodes = current_k8s_corev1_api_client.list_node()
            for node in nodes.items:
                result[node.metadata.name] = {
                    "capacity": node.status.capacity["memory"]
                }

            node_metrics = (
                current_k8s_custom_objects_api_client.list_cluster_custom_object(
                    "metrics.k8s.io", "v1beta1", "nodes"
                )
            )
            for node_metric in node_metrics.get("items", []):
                node_name = node_metric["metadata"]["name"]
                result[node_name]["usage"] = node_metric["usage"]["memory"]

                node_capacity = result[node_name]["capacity"]
                node_usage = result[node_name]["usage"]

                node_usage_bytes = kubernetes_memory_to_bytes(node_usage)
                node_capacity_bytes = kubernetes_memory_to_bytes(node_capacity)
                node_usage_percentage = ClusterHealth.get_percentage(
                    node_usage_bytes,
                    node_capacity_bytes,
                )
                result[node_name]["percentage"] = f"{node_usage_percentage}%"
                result[node_name]["available"] = node_capacity_bytes - node_usage_bytes
        except ApiException as e:
            msg = "Error while calling `metrics.k8s.io` API."
            logging.error(msg)
            logging.error(e)
            return {"error": msg}
        except ValueError as e:
            # FIXME: after new Kubernetes release this should be not needed
            msg = "Error while retreiving k8s list of nodes."
            logging.error(msg)
            logging.error(e)
            return {"error": msg}

        return result

    def get_available_memory(self):
        """Get list of available nodes memory."""
        nodes = self.get_memory_usage()
        if not nodes or "error" in nodes:
            # Cannot detect available memory; return empty list
            return []
        available_memory_information = [
            node.get("available") for node in nodes.values()
        ]
        if all(
            isinstance(n, float) or isinstance(n, int)
            for n in available_memory_information
        ):
            return available_memory_information
        else:
            # [None] values were detected on some Kubernetes 1.20 clusters with
            # older Kind versions on GNU/Linux; return empty list
            return []

    def get_friendly_memory_usage(self):
        """Get nodes email-friendly memory usage."""
        output_memory_usage = ""
        memory_usage = self.get_memory_usage()
        if "error" in memory_usage:
            return memory_usage["error"]
        if memory_usage:
            for node, memory in memory_usage.items():
                output_memory_usage += f"\n  {node}: {memory.get('usage')}/{memory.get('capacity')} ({memory.get('percentage')})"
        return output_memory_usage

    def get_status(self):
        """Get status summary for REANA nodes."""
        return {
            "unschedulable_nodes": self.get_unschedulable_nodes(),
            "memory_usage": self.get_friendly_memory_usage(),
        }


class PodsStatus(REANAStatus):
    """Class to retrieve statistics related to REANA cluster pods."""

    def __init__(self, from_=None, until=None, user=None):
        """Initialise PodStatus class.

        :param from_: From which moment in time to collect information. Not
            implemented yet.
        :param until: Until which moment in time to collect information. Not
            implemented yet.
        :param user: A REANA-DB user model.
        :type from_: datetime
        :type until: datetime
        :type user: reana_db.models.User
        """
        self.statuses = ["Running", "Pending", "Suceeded", "Failed", "Unknown"]
        super().__init__(from_=from_, until=until, user=user)

    def get_pods_by_status(self, status, namespace):
        """Get pod name list by status."""
        pods = current_k8s_corev1_api_client.list_namespaced_pod(
            namespace,
            field_selector=f"status.phase={status}",
        )
        return [pod.metadata.name for pod in pods.items]

    def get_friendly_pods_by_status(self, status, namespace):
        """Get pod name list by status."""
        pods = self.get_pods_by_status(status, namespace)

        return "\n  ".join(["", *pods])

    def get_status(self):
        """Get status summary for REANA pods."""
        return {
            f"{ns}_{status.lower()}_pods": self.get_friendly_pods_by_status(status, ns)
            for ns in self.namespaces
            for status in self.statuses
        }


class JobsStatus(REANAStatus):
    """Class to retrieve statistics related to REANA cluster jobs."""

    def __init__(self, from_=None, until=None, user=None):
        """Initialise PodStatus class.

        :param from_: From which moment in time to collect information. Not
            implemented yet.
        :param until: Until which moment in time to collect information. Not
            implemented yet.
        :param user: A REANA-DB user model.
        :type from_: datetime
        :type until: datetime
        :type user: reana_db.models.User
        """
        self.compute_backends = REANA_COMPUTE_BACKENDS.values()
        self.statuses = [
            JobStatus.running,
            JobStatus.finished,
            JobStatus.failed,
            JobStatus.queued,
        ]
        super().__init__(from_=from_, until=until, user=user)

    def get_jobs_by_status_and_compute_backend(self, status, compute_backend=None):
        """Get the number of jobs in status ``status`` from ``compute_backend``."""
        query = Session.query(Job).filter(Job.status == status)
        if compute_backend:
            query = query.filter(Job.compute_backend == compute_backend)

        return query.count()

    def get_k8s_jobs_by_status(self, status):
        """Get from k8s API jobs in ``status`` status."""
        pods = current_k8s_corev1_api_client.list_namespaced_pod(
            REANA_RUNTIME_KUBERNETES_NAMESPACE,
            field_selector=f"status.phase={status}",
        )

        job_pods = [
            pod.metadata.name
            for pod in pods.items
            if pod.metadata.name.startswith(f"{REANA_COMPONENT_PREFIX}-run-job")
        ]

        return job_pods

    def get_status(self):
        """Get status summary for REANA jobs."""
        job_statuses = {
            compute_backend.lower(): {
                status.name: self.get_jobs_by_status_and_compute_backend(
                    status, compute_backend=compute_backend
                )
                for status in self.statuses
            }
            for compute_backend in self.compute_backends
        }

        job_statuses["kubernetes_api"] = {
            "running": len(self.get_k8s_jobs_by_status("Running")),
            "pending": len(self.get_k8s_jobs_by_status("Pending")),
        }
        return job_statuses

    def get_total_slots(self):
        """Get total amount of job slots available in REANA cluster."""
        total_cluster_memory = NodesStatus().get_total_memory()
        jobs_memory_limit = REANA_KUBERNETES_JOBS_MEMORY_LIMIT_IN_BYTES
        slots = (
            round(total_cluster_memory / jobs_memory_limit)
            if total_cluster_memory and jobs_memory_limit
            else None
        )
        return slots


class ClusterHealthStatus(enum.Enum):
    """Enumeration of cluster health statuses."""

    healthy = 0
    warning = 1
    critical = 2


class ClusterHealth:
    """Class to retrieve REANA cluster health information."""

    def __init__(self):
        """Initialise Cluster Health class."""
        self.node = self.get_node_health()
        self.job = self.get_job_health()
        self.workflow = self.get_workflow_health()
        self.session = self.get_session_health()

    @staticmethod
    def get_health_status(percentage):
        """Calculate quota health status based on cluster availability."""
        health = ClusterHealthStatus.healthy
        if percentage <= 50:
            health = ClusterHealthStatus.warning
        if percentage <= 25:
            health = ClusterHealthStatus.critical

        return health.name

    @staticmethod
    def get_percentage(used, total):
        """Get usage percentage based on ``used`` and ``total``."""
        percentage = 100 - round((used / total if total else 0) * 100)
        return percentage if percentage > 0 else 0

    @staticmethod
    def get_available(unavailable, total):
        """Get available cluster resources based on ``unavailable`` and ``total``."""
        available = total - unavailable
        return available if available > 0 else 0

    def get_node_health(self):
        """Get cluster nodes health information."""
        nodes_status_obj = NodesStatus()

        total = len(nodes_status_obj.get_nodes())
        unschedulable = len(nodes_status_obj.get_unschedulable_nodes())
        available = ClusterHealth.get_available(unschedulable, total)
        percentage = ClusterHealth.get_percentage(unschedulable, total)

        return {
            "available": available,
            "unschedulable": unschedulable,
            "total": total,
            "percentage": percentage,
            "health": ClusterHealth.get_health_status(percentage),
            "sort": 0,
        }

    def get_job_health(self):
        """Get cluster jobs health information."""
        jobs_status_obj = JobsStatus()

        running = len(jobs_status_obj.get_k8s_jobs_by_status("Running"))
        pending = len(jobs_status_obj.get_k8s_jobs_by_status("Pending"))
        used = running + pending
        total = jobs_status_obj.get_total_slots() or used
        available = ClusterHealth.get_available(used, total)
        percentage = ClusterHealth.get_percentage(used, total)

        return {
            "running": running,
            "pending": pending,
            "available": available,
            "total": total,
            "percentage": percentage,
            "health": ClusterHealth.get_health_status(percentage),
            "sort": 1,
        }

    def get_workflow_health(self):
        """Get cluster workflows health information."""
        wf_status_obj = WorkflowsStatus()

        running = wf_status_obj.get_workflows_by_status(RunStatus.running)
        queued = wf_status_obj.get_workflows_by_status(RunStatus.queued)
        pending = wf_status_obj.get_workflows_by_status(RunStatus.pending)
        total = REANA_MAX_CONCURRENT_BATCH_WORKFLOWS
        used = running + pending
        available = ClusterHealth.get_available(used, total)
        percentage = ClusterHealth.get_percentage(used, total)

        return {
            "running": running,
            "queued": queued,
            "pending": pending,
            "available": available,
            "total": total,
            "percentage": percentage,
            "health": ClusterHealth.get_health_status(percentage),
            "sort": 2,
        }

    def get_session_health(self):
        """Get cluster sessions health information."""
        session_status_obj = InteractiveSessionsStatus()

        return {"active": session_status_obj.get_active(), "sort": 3}


class ClusterHealthSchema(Schema):
    """Cluster health marshmallow schema."""

    node = fields.Dict(keys=fields.Str(), values=fields.Int())
    job = fields.Dict(keys=fields.Str(), values=fields.Int())
    workflow = fields.Dict(keys=fields.Str(), values=fields.Int())
    session = fields.Dict(keys=fields.Str(), values=fields.Int())


STATUS_OBJECT_TYPES = {
    "interactive-sessions": InteractiveSessionsStatus,
    "workflows": WorkflowsStatus,
    "users": UsersStatus,
    "system": SystemStatus,
    "storage": StorageStatus,
    "nodes": NodesStatus,
    "pods": PodsStatus,
    "jobs": JobsStatus,
    "quota-usage": QuotaUsageStatus,
}
"""High level REANA objects to extract information from."""
