# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server Workflow Execution Scheduler."""

import json
import logging

from reana_commons.config import MQ_DEFAULT_QUEUES
from reana_commons.consumer import BaseConsumer
from reana_commons.tasks import reana_ready
from reana_db.database import Session
from reana_db.models import Workflow, WorkflowStatus

from reana_server.api_client import current_rwc_api_client


class WorkflowExecutionScheduler(BaseConsumer):
    """Scheduler of workflow execution.

    Class responsible for consuming from the workflow-submission queue
    and scheduling workflows for execution based on policies and system
    availability.
    """

    def __init__(self, **kwargs):
        """Initialise the WorkflowExecutionScheduler class."""
        super(WorkflowExecutionScheduler, self).__init__(
            queue='workflow-submission', **kwargs)

    def get_consumers(self, Consumer, channel):
        """Implement providing kombu.Consumers with queues/callbacks."""
        return [Consumer(queues=self.queue, callbacks=[self.on_message],
                         accept=[self.message_default_format])]

    def on_message(self, workflow_submission, message):
        """On new workflow_submission event handler."""
        if reana_ready():
            message.ack()
            workflow_submission = json.loads(workflow_submission)
            logging.info('Starting queued workflow: {}'.
                         format(workflow_submission))
            workflow_submission['status'] = 'start'
            response, http_response = current_rwc_api_client.api.\
                set_workflow_status(**workflow_submission).result()
            http_response_json = http_response.json()
            if http_response.status_code == 200:
                workflow_uuid = http_response_json['workflow_id']
                status = http_response_json['status']
                Workflow.update_workflow_status(
                    Session, workflow_uuid, status)
        else:
            message.requeue()
