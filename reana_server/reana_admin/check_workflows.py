# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server administrative tool - check workflows command."""


import datetime
import logging
import sys
from typing import Optional, List, Dict, Tuple
from functools import partial

import click
from reana_commons.k8s.api_client import current_k8s_corev1_api_client
from reana_commons.config import REANA_RUNTIME_KUBERNETES_NAMESPACE
from reana_db.database import Session
from reana_db.models import (
    Workflow,
    RunStatus,
)
from kubernetes.client import V1Pod
from kubernetes.client.rest import ApiException
from sqlalchemy.orm import Query

from reana_server.reana_admin.consumer import CollectingConsumer


class WorkflowCheckFailed(Exception):
    """Error when workflow doesn't pass some check."""


WorkflowCheckResult = Tuple[Workflow, List[WorkflowCheckFailed]]


def _display(validation_results: List[WorkflowCheckResult]):
    for workflow_result in validation_results:
        workflow = workflow_result[0]
        click.secho(
            f"- id: {str(workflow.id_)}, name: {workflow.name}, user: {workflow.owner.email}, status: {workflow.status}"
        )

        failed_checks = workflow_result[1]
        for check_error in failed_checks:
            click.secho(f"   - ERROR: {check_error}\n", fg="red")


def _get_all_pods() -> List[V1Pod]:
    try:
        response = current_k8s_corev1_api_client.list_namespaced_pod(
            namespace=REANA_RUNTIME_KUBERNETES_NAMESPACE
        )
        return response.items
    except ApiException as error:
        click.secho(
            "Couldn't fetch list of pods due to an error.",
            fg="red",
        )
        logging.exception(error)
        return []


def _collect_messages_from_scheduler_queue(filtered_workflows: Query) -> Dict:
    scheduler_messages_collector = CollectingConsumer(
        queue_name="workflow-submission",
        key="workflow_id_or_name",
        values_to_collect=[str(workflow.id_) for workflow in filtered_workflows],
    )
    scheduler_messages_collector.run()
    return scheduler_messages_collector.messages


def _message_is_in_scheduler_queue(workflow, pods, scheduler_messages):
    if workflow.id_ not in scheduler_messages:
        raise WorkflowCheckFailed("Message is not found in workflow-submission queue.")


def _pods_dont_exist(workflow, pods, scheduler_messages):
    if len(pods) > 0:
        raise WorkflowCheckFailed("Some pods still exist for the workflow.")


def _pods_exist(workflow, pods, scheduler_messages):
    if len(pods) == 0:
        raise WorkflowCheckFailed("No pods found for the workflow.")


def _all_batch_pods_have_phase(workflow, pods, scheduler_messages, phase):
    if any(
        [pod.status.phase != phase for pod in pods if "batch-pod" in pod.metadata.name]
    ):
        raise WorkflowCheckFailed(f"Some pods are not in {phase}.")


def _no_batch_pods_are_in_notready_state(workflow, pods, scheduler_messages):
    for pod in pods:
        if "batch-pod" in pod.metadata.name:
            for container in pod.status.container_statuses:
                if container.state.terminated:
                    raise WorkflowCheckFailed(
                        f"{pod.metadata.name} pod is in NotReady state."
                    )


validation_map = {
    RunStatus.queued: [
        _message_is_in_scheduler_queue,
    ],
    RunStatus.pending: [
        _pods_exist,
        partial(_all_batch_pods_have_phase, phase="Pending"),
    ],
    RunStatus.running: [
        _pods_exist,
        partial(_all_batch_pods_have_phase, phase="Running"),
        _no_batch_pods_are_in_notready_state,
    ],
    RunStatus.finished: [
        _pods_dont_exist,
    ],
    RunStatus.failed: [
        _pods_dont_exist,
    ],
}


def check_workflows(
    date_start: datetime.datetime, date_end: Optional[datetime.datetime]
) -> None:
    """Check if selected workflows are in sync with database according to predefined rules."""
    # filter workflows that need to be checked
    statuses_to_check = validation_map.keys()
    query_filter = [
        Workflow.status.in_(statuses_to_check),
        Workflow.created >= date_start,
    ]

    if date_end:
        query_filter.append(Workflow.created < date_end)

    filtered_workflows = Session.query(Workflow).filter(*query_filter)

    time_range_message = f"{date_start} - {date_end if date_end else 'now'}"

    if filtered_workflows.count() == 0:
        click.secho(
            f"No workflows found matching time range {time_range_message}.\nPlease, adjust your filter conditions.",
            fg="red",
        )
        sys.exit(1)
    click.secho(
        f"Found {filtered_workflows.count()} workflow(s) matching time range {time_range_message}.\n",
    )

    try:
        click.secho("Collecting MQ messages...")
        scheduler_messages = _collect_messages_from_scheduler_queue(filtered_workflows)
    except Exception as error:
        click.secho(
            "Error is raised when collecting scheduler messages.",
            fg="red",
        )
        logging.exception(error)
        sys.exit(1)

    click.secho("Collecting information about Pods...")
    pods = _get_all_pods()
    if not pods:
        click.secho(
            "List of active pods is empty. It is required to validate workflows. Exiting..",
            fg="red",
        )
        sys.exit(1)

    in_sync_workflows: List[WorkflowCheckResult] = []
    out_of_sync_workflows: List[WorkflowCheckResult] = []
    for workflow in filtered_workflows:
        filtered_pods = [pod for pod in pods if str(workflow.id_) in pod.metadata.name]

        checks = validation_map[workflow.status]
        failed_checks: List[WorkflowCheckFailed] = []

        workflow_is_in_sync = True
        for check in checks:
            try:
                check(workflow, filtered_pods, scheduler_messages)
            except WorkflowCheckFailed as error:
                failed_checks.append(error)
                workflow_is_in_sync = False

        if workflow_is_in_sync:
            in_sync_workflows.append((workflow, failed_checks))
        else:
            out_of_sync_workflows.append((workflow, failed_checks))

    if in_sync_workflows:
        click.secho(
            f"\nIn-sync workflows ({len(in_sync_workflows)} out of {filtered_workflows.count()})\n",
            fg="green",
        )
        _display(in_sync_workflows)

    if out_of_sync_workflows:
        click.secho(
            f"\nOut-of-sync workflows ({len(out_of_sync_workflows)} out of {filtered_workflows.count()})\n",
            fg="red",
        )
        _display(out_of_sync_workflows)

    if out_of_sync_workflows:
        click.secho("\nFAILED")
        sys.exit(1)
    else:
        click.secho("\nOK")
