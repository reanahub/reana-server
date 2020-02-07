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

from bravado.exception import HTTPBadGateway, HTTPNotFound
from reana_commons.consumer import BaseConsumer
from reana_commons.tasks import reana_ready
from reana_db.database import Session
from reana_db.models import Workflow

from reana_server.api_client import (current_rwc_api_client,
                                     current_workflow_submission_publisher)


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

    def requeue_workflow(self, **kwargs):
        """Send a workflow back to the queue.

        We do not use ``message.requeue()`` because it cannot be used after
        ``message.ack()``, and we cannot wait to validate all the checks
        (``reana_ready()`` and calling RWC) without calling ``message.ack()``
        and getting a new call to on_message with the same workflow.
        """
        try:
            current_workflow_submission_publisher.publish_workflow_submission(
                kwargs['user'], kwargs['workflow_id_or_name'],
                kwargs['parameters']
            )
            logging.error(
                f'Requeueing workflow '
                f'{kwargs["workflow_id_or_name"]} ...')
        except KeyError:
            logging.error(
                f'Wrong parameters to requeue workflow:\n'
                f'{kwargs}\n'
                f'Did reana_commons.publisher.WorkflowSubmissionPublisher\'s '
                f'method publish_workflow_submission change its signature?',
                exc_info=True)
        except Exception:
            logging.error('An error has occurred while requeueing worfklow',
                          exc_info=True)

    def on_message(self, workflow_submission, message):
        """On new workflow_submission event handler."""
        message.ack()
        workflow_submission = json.loads(workflow_submission)
        if reana_ready():
            logging.info('Starting queued workflow: {}'.
                         format(workflow_submission))
            workflow_submission['status'] = 'start'
            try:
                requeue = True
                started = False
                response, http_response = current_rwc_api_client.api.\
                    set_workflow_status(**workflow_submission).result()
                http_response_json = http_response.json()
                if http_response.status_code == 200:
                    started = True
                    logging.info(
                        f'Workflow '
                        f'{http_response_json["workflow_id"]} '
                        f'successfully started.')
                else:
                    logging.error(f'RWC returned an unexpected status code:\n'
                                  f'{http_response_json}')

            except HTTPBadGateway as api_e:
                logging.error(f'Workflow failed to start because '
                              f'RWC got an error while calling an external'
                              f'service (i.e. DB):\n'
                              f'{api_e}', exc_info=True)
            except HTTPNotFound as not_found_e:
                logging.error(f'Workflow failed to start because '
                              f'workflow does not exist or was deleted \n'
                              f'{not_found_e}', exc_info=True)
                requeue = False
            except Exception as e:
                logging.error(f'Something went wrong while calling RWC :\n'
                              f'{e}', exc_info=True)
            finally:
                if not started and requeue:
                    self.requeue_workflow(**workflow_submission)
        else:
            logging.info(f'REANA not ready to run workflow '
                         f'{workflow_submission["workflow_id_or_name"]}, '
                         f'requeueing ...')
            self.requeue_workflow(**workflow_submission)
