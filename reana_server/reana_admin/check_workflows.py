# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server administrative tool - check workflows command."""


import datetime
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass
from functools import partial

import click
from reana_commons.k8s.api_client import current_k8s_corev1_api_client
from reana_commons.config import REANA_RUNTIME_KUBERNETES_NAMESPACE
from reana_db.database import Session
from reana_db.models import (
    InteractiveSession,
    Workflow,
    RunStatus,
)
from kubernetes.client import V1Pod
from kubernetes.client.rest import ApiException
from sqlalchemy.orm import Query

from reana_server.reana_admin.consumer import CollectingConsumer


class CheckFailed(Exception):
    """Error when a check for workflow or session fails."""


class InfoCollectionError(Exception):
    """Error when check functions fails to query workflows/sessions, collect pods or queue messages."""


@dataclass
class CheckSource:
    """Contains metadata about workflow or session."""

    id: str
    name: str
    user: Optional[str]
    status: Optional[RunStatus]


@dataclass
class CheckResult:
    """Contains information about source and its errors (if any)."""

    source: CheckSource
    errors: List[CheckFailed]


def display_results(validation_results: List[CheckResult]):
    """Display results from a list of CheckResults."""
    for result in validation_results:
        source = result.source
        click.secho(
            f"- id: {source.id}, name: {source.name}, user: {source.user}, status: {source.status}"
        )

        failed_checks = result.errors
        for check_error in failed_checks:
            click.secho(f"   - ERROR: {check_error}\n", fg="red")


def _get_all_pods() -> List[V1Pod]:
    """Collect information about Pods.

    :raises kubernetes.client.rest.ApiException: in case of a k8s server error.
    """
    click.secho("Collecting information about Pods...")
    response = current_k8s_corev1_api_client.list_namespaced_pod(
        namespace=REANA_RUNTIME_KUBERNETES_NAMESPACE
    )
    return response.items


def _collect_messages_from_scheduler_queue(filtered_workflows: Query) -> Dict:
    scheduler_messages_collector = CollectingConsumer(
        queue_name="workflow-submission",
        key="workflow_id_or_name",
        values_to_collect=[str(workflow.id_) for workflow in filtered_workflows],
    )
    scheduler_messages_collector.run()
    return scheduler_messages_collector.messages


def _message_is_in_scheduler_queue(workflow, pods, scheduler_messages):
    if str(workflow.id_) not in scheduler_messages:
        raise CheckFailed("Message is not found in workflow-submission queue.")


def _pods_dont_exist(workflow, pods, scheduler_messages):
    if len(pods) > 0:
        raise CheckFailed("Some pods still exist.")


def _pods_exist(workflow, pods, scheduler_messages):
    if len(pods) == 0:
        raise CheckFailed("No pods found.")


def _only_one_pod_exists(workflow, pods, scheduler_messages):
    if len(pods) != 1:
        raise CheckFailed(f"Only one pod should exist. Found {len(pods)}.")


def _all_pods_have_phase(workflow, pods, scheduler_messages, phase):
    if any([pod.status.phase != phase for pod in pods]):
        raise CheckFailed(f"Some pods are not in {phase}.")


def _no_batch_pods_are_in_notready_state(workflow, pods, scheduler_messages):
    for pod in pods:
        for container in pod.status.container_statuses:
            if container.state.terminated:
                raise CheckFailed(f"{pod.metadata.name} pod is in NotReady state.")


validation_map = {
    RunStatus.queued: [
        _message_is_in_scheduler_queue,
    ],
    RunStatus.pending: [
        _pods_exist,
        partial(_all_pods_have_phase, phase="Pending"),
    ],
    RunStatus.running: [
        _pods_exist,
        partial(_all_pods_have_phase, phase="Running"),
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
) -> Tuple[List[CheckResult], List[CheckResult], int]:
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

    total_workflows = filtered_workflows.count()

    if total_workflows == 0:
        click.secho(
            f"No workflows found matching time range {time_range_message}.",
            fg="red",
        )
        return [], [], total_workflows
    click.secho(
        f"Found {total_workflows} workflow(s) matching time range {time_range_message}.",
    )

    try:
        click.secho("Collecting MQ messages...")
        scheduler_messages = _collect_messages_from_scheduler_queue(filtered_workflows)
    except Exception as error:
        raise InfoCollectionError(f"Couldn't collect scheduler messages: {error}")

    try:
        pods = _get_all_pods()
    except ApiException as error:
        raise InfoCollectionError(f"Couldn't fetch list of pods: {error}")

    in_sync_workflows: List[CheckResult] = []
    out_of_sync_workflows: List[CheckResult] = []
    for workflow in filtered_workflows:
        part_of_pod_id = f"run-batch-{workflow.id_}"
        filtered_pods = [pod for pod in pods if part_of_pod_id in pod.metadata.name]

        checks = validation_map[workflow.status]
        failed_checks: List[CheckFailed] = []

        workflow_is_in_sync = True
        for check in checks:
            try:
                check(workflow, filtered_pods, scheduler_messages)
            except CheckFailed as error:
                failed_checks.append(error)
                workflow_is_in_sync = False

        check_source = CheckSource(
            id=str(workflow.id_),
            name=workflow.get_full_workflow_name(),
            user=workflow.owner.email,
            status=workflow.status,
        )

        if workflow_is_in_sync:
            in_sync_workflows.append(CheckResult(check_source, failed_checks))
        else:
            out_of_sync_workflows.append(CheckResult(check_source, failed_checks))

    return in_sync_workflows, out_of_sync_workflows, total_workflows


interactive_sessions_validation_map = {
    RunStatus.created: [
        _only_one_pod_exists,
        partial(_all_pods_have_phase, phase="Running"),
    ]
}


def check_interactive_sessions() -> (
    Tuple[List[CheckResult], List[CheckResult], List[CheckResult], int]
):
    """Check if selected sessions are in sync with database according to predefined rules.

    Because interactive sessions are deleted from database when they are closed,
    the function queries all of them at once.
    The number of interactive sessions is usually low (<100) so applying time range doesn't make sense.
    In addition, session pods can still exist even if session is not present in the database.
    So, it is easier to query all sessions to determine if some pods are hanging.
    """
    statuses_to_check = interactive_sessions_validation_map.keys()
    filtered_sessions = Session.query(InteractiveSession).filter(
        InteractiveSession.status.in_(statuses_to_check)
    )

    total_sessions = filtered_sessions.count()

    click.secho(
        f"Found {total_sessions} interactive session(s) in the database.",
    )

    try:
        pods = _get_all_pods()
    except ApiException as error:
        raise InfoCollectionError(f"Couldn't fetch list of pods: {error}")

    in_sync_sessions: List[CheckResult] = []
    out_of_sync_sessions: List[CheckResult] = []
    pods_without_session: List[CheckResult] = []

    # check if some session pods don't have corresponding session entry in the database
    session_pods = [pod for pod in pods if "run-session" in pod.metadata.name]
    for pod in session_pods:
        pod_doesnt_have_session = not any(
            [session.name in pod.metadata.name for session in filtered_sessions]
        )
        if pod_doesnt_have_session:
            check_source = CheckSource(
                id=pod.metadata.name, name="Session pod", user=None, status=None
            )
            failed_checks = [CheckFailed("Pod doesn't have a session in the database.")]
            pods_without_session.append(CheckResult(check_source, failed_checks))

    # check if sessions in the database pass checks
    for session in filtered_sessions:
        filtered_pods = [
            pod for pod in session_pods if str(session.name) in pod.metadata.name
        ]

        checks = interactive_sessions_validation_map[session.status]
        failed_checks: List[CheckFailed] = []
        session_is_in_sync = True

        for check in checks:
            try:
                check(session, filtered_pods, [])
            except CheckFailed as error:
                failed_checks.append(error)
                session_is_in_sync = False
                break

        check_source = CheckSource(
            id=str(session.id_),
            name=session.name,
            user=str(session.workflow[0].owner.email),
            status=session.status,
        )

        if session_is_in_sync:
            in_sync_sessions.append(CheckResult(check_source, failed_checks))
        else:
            out_of_sync_sessions.append(CheckResult(check_source, failed_checks))

    return in_sync_sessions, out_of_sync_sessions, pods_without_session, total_sessions
