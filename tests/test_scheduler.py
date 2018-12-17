# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server Workflow Execution Scheduler."""

import json
import threading

import pytest
from kombu import Exchange, Queue
from mock import ANY, patch
from reana_commons.config import MQ_DEFAULT_QUEUES
from reana_commons.publisher import WorkflowSubmissionPublisher
from requests.exceptions import ConnectionError

from reana_server.scheduler import WorkflowExecutionScheduler


def test_scheduler_starts_workflows(in_memory_queue_connection,
                                    default_in_memory_producer,
                                    consume_queue):
    """Test message is consumed from the queue."""
    scheduler = WorkflowExecutionScheduler(
        connection=in_memory_queue_connection)

    in_memory_workflow_submission_publisher = WorkflowSubmissionPublisher(
        connection=in_memory_queue_connection)
    in_memory_workflow_submission_publisher.publish_workflow_submission(
        '1', 'workflow.1', {}
    )
    with patch('reana_commons.config.REANA_READY_CONDITIONS',
               {'pytest_reana.fixtures':
                ['sample_condition_for_starting_queued_workflows']}):
        with pytest.raises(ConnectionError):
            consume_queue(scheduler, limit=1)
    assert in_memory_queue_connection.channel().queues[
        'workflow-submission'].empty()


def test_scheduler_requeues_workflows(in_memory_queue_connection,
                                      default_in_memory_producer,
                                      consume_queue):
    """Test that the scheduler requeues workflows if conditions not met."""
    scheduler = WorkflowExecutionScheduler(
        connection=in_memory_queue_connection)

    in_memory_workflow_submission_publisher = WorkflowSubmissionPublisher(
        connection=in_memory_queue_connection)
    in_memory_workflow_submission_publisher.publish_workflow_submission(
        '1', 'workflow.1', {}
    )
    with patch('reana_commons.config.REANA_READY_CONDITIONS',
               {'pytest_reana.fixtures':
                ['sample_condition_for_requeueing_workflows']}):
        consume_queue(scheduler, limit=1)
        assert not in_memory_queue_connection.channel().queues[
            'workflow-submission'].empty()
