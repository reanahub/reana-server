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

from reana_db.models import RunStatus, Workflow

from reana_server.api_client import WorkflowSubmissionPublisher
from reana_server.complexity import HYBRID_KUBERNETES_COMPLEXITY_MARKER
from reana_server.scheduler import (
    WorkflowExecutionScheduler,
    check_concurrent_workflows_limit,
)

# Complexity rows are (job_count, memory_bytes); a job_count of 0 means the step
# runs on an external backend (e.g. HTCondor) and >0 means it runs on Kubernetes.
K8S_COMPLEXITY = [(1, 1024)]
EXTERNAL_COMPLEXITY = [(0, 1024)]
# Stored complexity of a hybrid workflow: the initial steps are all external, so
# publish_workflow_submission appends HYBRID_KUBERNETES_COMPLEXITY_MARKER (see
# reana_server.complexity.get_complexity_to_store) to record that a later step
# runs on Kubernetes.
HYBRID_COMPLEXITY = EXTERNAL_COMPLEXITY + [HYBRID_KUBERNETES_COMPLEXITY_MARKER]


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


def _add_workflow(session, owner_id, status, complexity):
    """Persist a workflow with the given status and complexity."""
    workflow = Workflow(
        id_=uuid4(),
        name="test_workflow",
        owner_id=owner_id,
        reana_specification={},
        type_="serial",
        status=status,
        complexity=complexity,
    )
    session.add(workflow)
    session.commit()
    return workflow


@pytest.mark.parametrize(
    "workflows,max_concurrent,expect_error",
    [
        # Kubernetes workflows count towards the limit.
        ([(RunStatus.running, K8S_COMPLEXITY)] * 2, 2, True),
        ([(RunStatus.running, K8S_COMPLEXITY)], 2, False),
        # Pending Kubernetes workflows count as well.
        ([(RunStatus.pending, K8S_COMPLEXITY)], 1, True),
        # External-only workflows do not count towards the limit.
        ([(RunStatus.running, EXTERNAL_COMPLEXITY)] * 5, 1, False),
        # Hybrid workflows (external initial steps, Kubernetes later) keep
        # consuming a slot thanks to the stored marker row.
        ([(RunStatus.running, HYBRID_COMPLEXITY)] * 2, 2, True),
        ([(RunStatus.pending, HYBRID_COMPLEXITY)], 1, True),
        # Mixed: only the single Kubernetes workflow is counted.
        (
            [(RunStatus.running, EXTERNAL_COMPLEXITY)] * 3
            + [(RunStatus.running, K8S_COMPLEXITY)],
            2,
            False,
        ),
        # Empty/unknown complexity is counted conservatively.
        ([(RunStatus.running, [])], 1, True),
        # Non-pending/running workflows are ignored regardless of complexity.
        ([(RunStatus.finished, K8S_COMPLEXITY)] * 5, 1, False),
    ],
)
def test_check_concurrent_workflows_limit_counts_only_kubernetes(
    session, user0, workflows, max_concurrent, expect_error
):
    """Only pending/running workflows with a Kubernetes step are counted."""
    for status, complexity in workflows:
        _add_workflow(session, user0.id_, status, complexity)

    with patch(
        "reana_server.scheduler.REANA_MAX_CONCURRENT_BATCH_WORKFLOWS", max_concurrent
    ):
        error = check_concurrent_workflows_limit()

    assert (error is not None) == expect_error


def test_check_concurrent_workflows_limit_bypassed_for_external(session, user0):
    """External-only submissions bypass the check without querying the DB."""
    for _ in range(5):
        _add_workflow(session, user0.id_, RunStatus.running, K8S_COMPLEXITY)

    with patch("reana_server.scheduler.REANA_MAX_CONCURRENT_BATCH_WORKFLOWS", 1):
        assert check_concurrent_workflows_limit(uses_kubernetes=False) is None


@patch("reana_server.complexity.REANA_KUBERNETES_JOBS_MEMORY_LIMIT", "4Gi")
@patch("reana_server.utils.current_workflow_submission_publisher")
def test_hybrid_workflow_submission_consumes_concurrency_slot(
    mock_publisher, session, user0
):
    """A hybrid workflow (external first, Kubernetes later) must keep a slot.

    Its initial-step complexity contains no Kubernetes jobs, so the stored
    complexity must carry the hybrid marker row for the concurrent-workflows
    check to keep counting the workflow after submission.
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
                        {"commands": ["echo external"], "compute_backend": "htcondor"},
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

    # The submission message classifies the workflow as using Kubernetes and the
    # stored complexity records it with a positive job count.
    _, kwargs = mock_publisher.publish_workflow_submission.call_args
    assert kwargs["uses_kubernetes"] is True
    assert any(jobs > 0 for jobs, _ in workflow.complexity)

    # Once the workflow is pending/running, it consumes a concurrency slot.
    Workflow.update_workflow_status(session, workflow.id_, RunStatus.running)
    with patch("reana_server.scheduler.REANA_MAX_CONCURRENT_BATCH_WORKFLOWS", 1):
        assert check_concurrent_workflows_limit() is not None
