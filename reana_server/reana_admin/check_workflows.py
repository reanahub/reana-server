# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server administrative tool - check workflows command."""


import datetime
from pathlib import Path
from typing import Optional, List, Dict, Set, Tuple
from dataclasses import dataclass, asdict
from functools import partial

import click
from reana_commons.k8s.api_client import current_k8s_corev1_api_client
from reana_commons.config import REANA_RUNTIME_KUBERNETES_NAMESPACE
from reana_db.database import Session
from reana_db.models import InteractiveSession, Workflow, RunStatus, User
from kubernetes.client import V1Pod
from kubernetes.client.rest import ApiException
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Query
from reana_server.config import SHARED_VOLUME_PATH

from reana_server.reana_admin.consumer import CollectingConsumer


class CheckFailed(Exception):
    """Error when a check for workflow or session fails."""


class InfoCollectionError(Exception):
    """Error when check functions fails to query workflows/sessions, collect pods or queue messages."""


@dataclass
class CheckSource:
    """Contains metadata about workflow or session."""

    id: Optional[str]
    name: Optional[str]
    user: Optional[str]
    status: Optional[RunStatus]
    workspace: Optional[str]


@dataclass
class CheckResult:
    """Contains information about source and its errors (if any)."""

    source: CheckSource
    errors: List[CheckFailed]


def display_results(
    validation_results: List[CheckResult],
    headers: List[str] = ["id", "name", "user", "status"],
):
    """Display results from a list of CheckResults."""
    for result in validation_results:
        source = asdict(result.source)
        click.secho(
            "- " + ", ".join(f"{header}: {source[header]}" for header in headers)
        )

        failed_checks = result.errors
        for check_error in failed_checks:
            click.secho(f"   - ERROR: {check_error}", fg="red")
        if failed_checks:
            click.echo("")


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


def _message_is_in_scheduler_queue(
    workflow: Workflow, pods: List[V1Pod], scheduler_messages: Dict
):
    if str(workflow.id_) not in scheduler_messages:
        raise CheckFailed("Message is not found in workflow-submission queue.")


def _pods_dont_exist(workflow: Workflow, pods: List[V1Pod], scheduler_messages: Dict):
    if len(pods) > 0:
        raise CheckFailed("Some pods still exist.")


def _pods_exist(workflow: Workflow, pods: List[V1Pod], scheduler_messages: Dict):
    if len(pods) == 0:
        raise CheckFailed("No pods found.")


def _only_one_pod_exists(
    workflow: Workflow, pods: List[V1Pod], scheduler_messages: Dict
):
    if len(pods) != 1:
        raise CheckFailed(f"Only one pod should exist. Found {len(pods)}.")


def _all_pods_have_phase(
    workflow: Workflow, pods: List[V1Pod], scheduler_messages: Dict, phase
):
    if any([pod.status.phase != phase for pod in pods]):
        raise CheckFailed(f"Some pods are not in {phase}.")


def _no_batch_pods_are_in_notready_state(
    workflow: Workflow, pods: List[V1Pod], scheduler_messages: Dict
):
    for pod in pods:
        for container in pod.status.container_statuses:
            if container.state.terminated:
                raise CheckFailed(f"{pod.metadata.name} pod is in NotReady state.")


def _workspace_exists(workflow: Workflow, pods: List[V1Pod], scheduler_messages: Dict):
    if not Path(workflow.workspace_path).exists():
        raise CheckFailed(f"The workspace '{workflow.workspace_path}' does not exist.")


validation_map = {
    RunStatus.created: [
        _workspace_exists,
    ],
    RunStatus.queued: [
        _message_is_in_scheduler_queue,
        _workspace_exists,
    ],
    RunStatus.pending: [
        _pods_exist,
        partial(_all_pods_have_phase, phase="Pending"),
        _workspace_exists,
    ],
    RunStatus.running: [
        _pods_exist,
        partial(_all_pods_have_phase, phase="Running"),
        _no_batch_pods_are_in_notready_state,
        _workspace_exists,
    ],
    RunStatus.finished: [
        _pods_dont_exist,
        _workspace_exists,
    ],
    RunStatus.failed: [
        _pods_dont_exist,
        _workspace_exists,
    ],
    RunStatus.stopped: [
        _workspace_exists,
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
            workspace=workflow.workspace_path,
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
                id=pod.metadata.name,
                name="Session pod",
                user=None,
                status=None,
                workspace=None,
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
            workspace=None,
        )

        if session_is_in_sync:
            in_sync_sessions.append(CheckResult(check_source, failed_checks))
        else:
            out_of_sync_sessions.append(CheckResult(check_source, failed_checks))

    return in_sync_sessions, out_of_sync_sessions, pods_without_session, total_sessions


def check_workspaces() -> List[CheckResult]:
    """Check whether every workspace on disk is owned by a workflow in the database."""
    workspaces_on_disk: Set[Path] = set()
    for user_directory in Path(SHARED_VOLUME_PATH, "users").iterdir():
        try:
            workspaces_on_disk.update(user_directory.joinpath("workflows").iterdir())
        except FileNotFoundError:
            # "workflows" directory does not exist
            continue
    click.echo(
        f"Found {len(workspaces_on_disk)} workspace(s) on shared volume.",
    )

    workspaces_in_db = {
        Path(row[0]) for row in Session.query(Workflow.workspace_path.distinct())
    }

    extra_workspaces = []
    for extra_workspace_path in sorted(workspaces_on_disk - workspaces_in_db):
        # the last three parts of the workspace path are "<user_id>/workflows/<workflow_id>"
        user_id, _, workflow_id = extra_workspace_path.parts[-3:]

        # find possible user/workflow owner
        try:
            user = User.query.filter_by(id_=user_id).one_or_none()
        except StatementError:
            # user_id is not a valid UUID
            user = None
        try:
            workflow = Workflow.query.filter_by(id_=workflow_id).one_or_none()
        except StatementError:
            # workflow_id is not a valid UUID
            workflow = None

        source = CheckSource(
            id=str(workflow.id_) if workflow else None,
            name=workflow.name if workflow else None,
            user=user.email if user else None,
            status=workflow.status if workflow else None,
            workspace=str(extra_workspace_path),
        )

        failures = [CheckFailed("The workspace is not owned by any workflow.")]
        if workflow:
            failures.append(
                CheckFailed(
                    f"The related workflow has '{workflow.workspace_path}' as workspace instead."
                )
            )

        extra_workspaces.append(CheckResult(source, failures))

    return extra_workspaces
