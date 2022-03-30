# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021, 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server Workflow Execution Scheduler."""
import base64
import json

import pytest
from bravado.exception import HTTPBadGateway, HTTPNotFound, HTTPConflict
from mock import DEFAULT, Mock, patch

from reana_server.api_client import WorkflowSubmissionPublisher
from reana_server.scheduler import WorkflowExecutionScheduler


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
