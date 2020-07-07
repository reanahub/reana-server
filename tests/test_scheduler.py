# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server Workflow Execution Scheduler."""


from bravado.exception import HTTPError
from mock import DEFAULT, Mock, patch
from reana_commons.publisher import WorkflowSubmissionPublisher

from reana_server.scheduler import WorkflowExecutionScheduler


def test_scheduler_starts_workflows(
    in_memory_queue_connection, default_in_memory_producer, consume_queue
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
        reana_ready=Mock(return_value=True),
        current_rwc_api_client=mock_rwc_api_client,
        current_workflow_submission_publisher=in_memory_wsp,
    ):
        consume_queue(scheduler, limit=1)
    assert in_memory_queue_connection.channel().queues["workflow-submission"].empty()


def test_scheduler_requeues_workflows(
    in_memory_queue_connection, default_in_memory_producer, consume_queue
):
    """Test that the scheduler requeues workflows if conditions not met."""
    scheduler = WorkflowExecutionScheduler(connection=in_memory_queue_connection)

    in_memory_wsp = WorkflowSubmissionPublisher(connection=in_memory_queue_connection)
    in_memory_wsp.publish_workflow_submission("1", "workflow.1", {})
    with patch.multiple(
        "reana_server.scheduler",
        reana_ready=Mock(return_value=False),
        current_workflow_submission_publisher=in_memory_wsp,
    ):
        consume_queue(scheduler, limit=1)
        assert (
            not in_memory_queue_connection.channel()
            .queues["workflow-submission"]
            .empty()
        )


def test_scheduler_requeues_on_rwc_failure(
    in_memory_queue_connection, default_in_memory_producer, consume_queue
):
    """Test scheduler requeues requests if RWC fails to start workflows."""
    scheduler = WorkflowExecutionScheduler(connection=in_memory_queue_connection)

    in_memory_wsp = WorkflowSubmissionPublisher(connection=in_memory_queue_connection)
    in_memory_wsp.publish_workflow_submission("1", "workflow.1", {})
    mock_rwc_api_client = Mock()
    mock_result_obj = Mock()
    mock_response = Mock()
    mock_response.status_code = 502
    mock_result_obj.result = Mock(
        side_effect=HTTPError(mock_response, message="DB connection timed out.")
    )
    mock_rwc_api_client.api.set_workflow_status.return_value = mock_result_obj
    with patch.multiple(
        "reana_server.scheduler",
        reana_ready=Mock(return_value=True),
        current_rwc_api_client=mock_rwc_api_client,
        current_workflow_submission_publisher=in_memory_wsp,
    ):
        consume_queue(scheduler, limit=1)
        assert (
            not in_memory_queue_connection.channel()
            .queues["workflow-submission"]
            .empty()
        )
