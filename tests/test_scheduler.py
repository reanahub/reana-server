# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021, 2022, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server Workflow Execution Scheduler."""

import base64
import json
from uuid import uuid4

import pytest
from bravado.exception import HTTPBadGateway, HTTPNotFound, HTTPConflict
from mock import DEFAULT, Mock, patch

from reana_db.models import RunStatus, Service, ServiceStatus, ServiceType, Workflow

from reana_server.api_client import WorkflowSubmissionPublisher
from reana_server.scheduler import (
    WorkflowExecutionScheduler,
    check_concurrent_workflows_limit,
)


def _patch_caps(per_backend=None, k8s=30, external=200, dask_cap=5):
    """Patch the concurrency caps resolved by ``get_concurrent_workflows_cap``.

    :param per_backend: JSON-style override map of backend identifier -> cap.
    :param k8s: default Kubernetes cap.
    :param external: default cap for external backends without an override.
    :param dask_cap: Dask-cluster cap.
    """
    return patch.multiple(
        "reana_commons.config",
        REANA_MAX_CONCURRENT_BATCH_WORKFLOWS_PER_BACKEND=per_backend or {},
        REANA_MAX_CONCURRENT_K8S_BATCH_WORKFLOWS=k8s,
        REANA_MAX_CONCURRENT_EXTERNAL_BATCH_WORKFLOWS=external,
        REANA_MAX_CONCURRENT_DASK_WORKFLOWS=dask_cap,
    )


def test_scheduler_starts_workflows(
    in_memory_queue_connection,
    default_in_memory_producer,
    consume_queue,
):
    """Test message is consumed from the queue."""
    workflow_name = "workflow.1"
    scheduler = WorkflowExecutionScheduler(connection=in_memory_queue_connection)
    in_memory_wsp = WorkflowSubmissionPublisher(connection=in_memory_queue_connection)

    in_memory_wsp.publish_workflow_submission("1", workflow_name, {})
    mock_rwc_api_client = Mock()
    mock_result_obj = Mock()
    mock_response = Mock()
    mock_response.status_code = 200
    mock_result_obj.result.return_value = (DEFAULT, mock_response)
    mock_rwc_api_client.api.set_workflow_status.return_value = mock_result_obj
    with patch.multiple(
        "reana_server.scheduler",
        reana_ready=Mock(return_value=None),
        current_rwc_api_client=mock_rwc_api_client,
        REANA_SCHEDULER_REQUEUE_SLEEP=0,
    ):
        consume_queue(scheduler, limit=1)
    assert in_memory_queue_connection.channel().queues["workflow-submission"].empty()


def test_scheduler_requeues_when_not_ready(
    in_memory_queue_connection,
    default_in_memory_producer,
    consume_queue,
):
    """Test that the scheduler requeues workflows if conditions not met."""
    scheduler = WorkflowExecutionScheduler(connection=in_memory_queue_connection)
    in_memory_wsp = WorkflowSubmissionPublisher(connection=in_memory_queue_connection)

    in_memory_wsp.publish_workflow_submission("1", "workflow.1", {})
    with patch.multiple(
        "reana_server.scheduler",
        reana_ready=Mock(return_value="error"),
        current_workflow_submission_publisher=in_memory_wsp,
        REANA_SCHEDULER_REQUEUE_SLEEP=0,
    ):
        consume_queue(scheduler, limit=1)
        assert (
            not in_memory_queue_connection.channel()
            .queues["workflow-submission"]
            .empty()
        )
        message = (
            in_memory_queue_connection.channel().queues["workflow-submission"].get()
        )
        message_body = base64.b64decode(message["body"]).decode("ascii")
        message_body = json.loads(json.loads(message_body))
        assert message_body["retry_count"] == 1


@pytest.mark.parametrize(
    "error,should_retry",
    [
        (HTTPBadGateway(Mock()), True),
        (Exception(Mock()), True),
        (HTTPNotFound(Mock()), False),
        (HTTPConflict(Mock()), False),
    ],
)
def test_scheduler_requeues_on_rwc_failure(
    in_memory_queue_connection,
    default_in_memory_producer,
    consume_queue,
    error,
    should_retry,
):
    """Test scheduler requeues requests if RWC fails to start workflows."""
    scheduler = WorkflowExecutionScheduler(connection=in_memory_queue_connection)
    in_memory_wsp = WorkflowSubmissionPublisher(connection=in_memory_queue_connection)

    in_memory_wsp.publish_workflow_submission("1", "workflow.1", {})
    mock_rwc_api_client = Mock()
    mock_result_obj = Mock()
    mock_result_obj.result = Mock(side_effect=error)
    mock_rwc_api_client.api.set_workflow_status.return_value = mock_result_obj
    with patch.multiple(
        "reana_server.scheduler",
        reana_ready=Mock(return_value=None),
        current_rwc_api_client=mock_rwc_api_client,
        current_workflow_submission_publisher=in_memory_wsp,
        REANA_SCHEDULER_REQUEUE_SLEEP=0,
    ):
        consume_queue(scheduler, limit=1)

        if should_retry:
            assert (
                not in_memory_queue_connection.channel()
                .queues["workflow-submission"]
                .empty()
            )
            message = (
                in_memory_queue_connection.channel().queues["workflow-submission"].get()
            )
            message_body = base64.b64decode(message["body"]).decode("ascii")
            message_body = json.loads(json.loads(message_body))
            assert message_body["retry_count"] == 1
        else:
            assert (
                in_memory_queue_connection.channel()
                .queues["workflow-submission"]
                .empty()
            )


def test_scheduler_fail_after_too_many_retries(
    in_memory_queue_connection,
    default_in_memory_producer,
    consume_queue,
):
    """Test scheduler requeues requests if RWC fails to start workflows."""
    scheduler = WorkflowExecutionScheduler(connection=in_memory_queue_connection)
    in_memory_wsp = WorkflowSubmissionPublisher(connection=in_memory_queue_connection)

    in_memory_wsp.publish_workflow_submission("1", "workflow.1", {})
    with patch.multiple(
        "reana_server.scheduler",
        reana_ready=Mock(return_value="error"),
        current_workflow_submission_publisher=in_memory_wsp,
        REANA_SCHEDULER_REQUEUE_SLEEP=0,
        REANA_SCHEDULER_REQUEUE_COUNT=1,
    ):
        consume_queue(scheduler, limit=1)
        assert (
            not in_memory_queue_connection.channel()
            .queues["workflow-submission"]
            .empty()
        )
        consume_queue(scheduler, limit=1)
        assert (
            in_memory_queue_connection.channel().queues["workflow-submission"].empty()
        )
        assert not in_memory_queue_connection.channel().queues["jobs-status"].empty()


def _add_workflow(
    session,
    owner_id,
    backend="kubernetes",
    status=RunStatus.running,
    uses_dask=False,
    compute_backends=None,
):
    """Persist a workflow with the given status, backends and Dask service."""
    workflow = Workflow(
        id_=uuid4(),
        name="test_workflow",
        owner_id=owner_id,
        reana_specification={},
        type_="serial",
        status=status,
        compute_backends=compute_backends or [backend],
    )
    if uses_dask:
        workflow.services.append(
            Service(
                name=f"dask-{workflow.id_}",
                uri=f"/{workflow.id_}/dashboard",
                type_=ServiceType.dask,
                status=ServiceStatus.created,
            )
        )
    session.add(workflow)
    session.commit()
    return workflow


def test_kubernetes_workflows_are_capped(session, user0):
    """Pending and running Kubernetes workflows count towards the k8s cap."""
    _add_workflow(session, user0.id_, "kubernetes", RunStatus.running)
    _add_workflow(session, user0.id_, "kubernetes", RunStatus.pending)

    with _patch_caps(k8s=2):
        assert check_concurrent_workflows_limit(["kubernetes"]) is not None
    with _patch_caps(k8s=3):
        assert check_concurrent_workflows_limit(["kubernetes"]) is None


def test_external_workflows_are_capped(session, user0):
    """External-only workflows now consume their own backend's cap (RWC663-02).

    Previously external workflows neither checked nor counted towards any cap, so
    a flood of HTCondor workflows could spawn unlimited orchestration pods. The
    cap applies even with no explicit override, via the external-backend default.
    """
    for _ in range(5):
        _add_workflow(session, user0.id_, "htcondorcern", RunStatus.running)

    # External-backend default cap.
    with _patch_caps(external=5):
        assert check_concurrent_workflows_limit(["htcondorcern"]) is not None
    with _patch_caps(external=6):
        assert check_concurrent_workflows_limit(["htcondorcern"]) is None
    # Per-backend override takes precedence over the default.
    with _patch_caps(per_backend={"htcondorcern": 5}, external=999):
        assert check_concurrent_workflows_limit(["htcondorcern"]) is not None


def test_unknown_backend_is_still_capped(session, user0):
    """A backend with no override falls back to the external default (no bypass)."""
    for _ in range(5):
        _add_workflow(session, user0.id_, "compute4punch", RunStatus.running)

    with _patch_caps(external=5):
        assert check_concurrent_workflows_limit(["compute4punch"]) is not None


def test_backend_caps_are_independent(session, user0):
    """A saturated external backend does not block other backends."""
    for _ in range(5):
        _add_workflow(session, user0.id_, "htcondorcern", RunStatus.running)

    # HTCondor is full, but a Kubernetes-only submission is unaffected.
    with _patch_caps(per_backend={"htcondorcern": 5}):
        assert check_concurrent_workflows_limit(["kubernetes"]) is None
        # A hybrid workflow that also uses HTCondor is blocked by the full cap.
        assert (
            check_concurrent_workflows_limit(["kubernetes", "htcondorcern"]) is not None
        )


def test_dask_workflows_are_capped(session, user0):
    """Dask workflows are bounded by the Dask cap regardless of step backend.

    A Dask-on-HTCondor workflow escapes the step-backend caps cheaply but still
    materialises a heavy Kubernetes Dask cluster, so the orthogonal Dask cap must
    apply (RWC663-01).
    """
    for _ in range(5):
        _add_workflow(
            session, user0.id_, "htcondorcern", RunStatus.running, uses_dask=True
        )

    # HTCondor cap is generous, but the Dask cap (5) is reached.
    with _patch_caps(external=200, dask_cap=5):
        assert (
            check_concurrent_workflows_limit(["htcondorcern"], uses_dask=True)
            is not None
        )
    with _patch_caps(external=200, dask_cap=6):
        assert (
            check_concurrent_workflows_limit(["htcondorcern"], uses_dask=True) is None
        )
        # A non-Dask submission on the same backend is not affected by the Dask cap.
        assert (
            check_concurrent_workflows_limit(["htcondorcern"], uses_dask=False) is None
        )


def test_only_active_workflows_are_counted(session, user0):
    """Finished/failed workflows do not count towards any cap."""
    for _ in range(5):
        _add_workflow(session, user0.id_, "kubernetes", RunStatus.finished)

    with _patch_caps(k8s=1):
        assert check_concurrent_workflows_limit(["kubernetes"]) is None


@patch("reana_server.utils.current_workflow_submission_publisher")
def test_hybrid_workflow_submission_classifies_all_backends(
    mock_publisher, session, user0
):
    """A hybrid workflow is classified with every backend it uses.

    Its initial steps run on HTCondor and later steps on Kubernetes, so the
    submission must record both backends and consume a slot in each cap.
    """
    from reana_server.utils import publish_workflow_submission

    workflow = Workflow(
        id_=uuid4(),
        name="hybrid_workflow",
        owner_id=user0.id_,
        reana_specification={
            "workflow": {
                "specification": {
                    "steps": [
                        {
                            "commands": ["echo external"],
                            "compute_backend": "htcondorcern",
                        },
                        {"commands": ["echo kubernetes"]},
                    ]
                }
            }
        },
        type_="serial",
        status=RunStatus.created,
    )
    session.add(workflow)
    session.commit()

    publish_workflow_submission(workflow, user0.id_, {})

    # The classification is persisted and sent along in the submission message.
    assert sorted(workflow.compute_backends) == ["htcondorcern", "kubernetes"]
    _, kwargs = mock_publisher.publish_workflow_submission.call_args
    assert sorted(kwargs["compute_backends"]) == ["htcondorcern", "kubernetes"]
    assert kwargs["uses_dask"] is False

    # Once pending/running, it consumes a slot in both backend caps.
    Workflow.update_workflow_status(session, workflow.id_, RunStatus.running)
    with _patch_caps(k8s=1):
        assert check_concurrent_workflows_limit(["kubernetes"]) is not None
    with _patch_caps(per_backend={"htcondorcern": 1}):
        assert check_concurrent_workflows_limit(["htcondorcern"]) is not None


@patch("reana_server.utils.current_workflow_submission_publisher")
def test_dask_on_external_workflow_submission_is_flagged(
    mock_publisher, session, user0
):
    """A Dask cluster on external steps must be flagged for the Dask cap (RWC663-01).

    The steps run on HTCondor (so ``compute_backends == ["htcondorcern"]``), but
    the workflow still requests a heavy Kubernetes Dask cluster, so ``uses_dask``
    must be reported so the scheduler applies the Dask cap.
    """
    from reana_server.utils import publish_workflow_submission

    workflow = Workflow(
        id_=uuid4(),
        name="dask_on_htcondor",
        owner_id=user0.id_,
        reana_specification={
            "workflow": {
                "type": "serial",
                "resources": {
                    "dask": {
                        "image": "daskdev/dask:latest",
                        "number_of_workers": 20,
                        "single_worker_memory": "800Mi",
                    }
                },
                "specification": {
                    "steps": [
                        {
                            "commands": ["echo external"],
                            "compute_backend": "htcondorcern",
                        },
                    ]
                },
            }
        },
        type_="serial",
        status=RunStatus.created,
    )
    session.add(workflow)
    session.commit()

    publish_workflow_submission(workflow, user0.id_, {})

    assert workflow.compute_backends == ["htcondorcern"]
    _, kwargs = mock_publisher.publish_workflow_submission.call_args
    assert kwargs["compute_backends"] == ["htcondorcern"]
    assert kwargs["uses_dask"] is True
