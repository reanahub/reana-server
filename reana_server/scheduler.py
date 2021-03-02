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
from time import sleep

from bravado.exception import HTTPBadGateway, HTTPNotFound
from kubernetes.client.rest import ApiException
from sqlalchemy.exc import SQLAlchemyError

from reana_commons.config import REANA_MAX_CONCURRENT_BATCH_WORKFLOWS
from reana_commons.consumer import BaseConsumer
from reana_commons.k8s.api_client import current_k8s_corev1_api_client
from reana_db.models import Workflow, WorkflowStatus

from reana_server.api_client import (
    current_rwc_api_client,
    current_workflow_submission_publisher,
)
from reana_server.config import REANA_SCHEDULER_SECONDS_TO_WAIT_FOR_REANA_READY


def check_predefined_conditions():
    """Check Kubernetes predefined conditions for the nodes."""
    try:
        node_info = json.loads(
            current_k8s_corev1_api_client.list_node(
                _preload_content=False
            ).data.decode()
        )
        for node in node_info["items"]:
            # check based on the predefined conditions about the
            # node status: MemoryPressure, OutOfDisk, KubeletReady
            #              DiskPressure, PIDPressure,
            for condition in node.get("status", {}).get("conditions", {}):
                if not condition.get("status"):
                    return False
    except ApiException as e:
        logging.error("Something went wrong while getting node information.")
        logging.error(e)
        return False
    return True


def doesnt_exceed_max_reana_workflow_count():
    """Check upper limit on running REANA batch workflows."""
    try:
        running_workflows = Workflow.query.filter_by(
            status=WorkflowStatus.running
        ).count()
        if running_workflows >= REANA_MAX_CONCURRENT_BATCH_WORKFLOWS:
            return False
    except SQLAlchemyError as e:
        logging.error(
            "Something went wrong while querying for number of running workflows."
        )
        logging.error(e)
        return False
    return True


def reana_ready():
    """Check if REANA can start new workflows."""
    for check_condition in [
        check_predefined_conditions,
        doesnt_exceed_max_reana_workflow_count,
    ]:
        if not check_condition():
            return False
    return True


class WorkflowExecutionScheduler(BaseConsumer):
    """Scheduler of workflow execution.

    Class responsible for consuming from the workflow-submission queue
    and scheduling workflows for execution based on policies and system
    availability.
    """

    def __init__(self, **kwargs):
        """Initialise the WorkflowExecutionScheduler class."""
        super(WorkflowExecutionScheduler, self).__init__(
            queue="workflow-submission", **kwargs
        )

    def get_consumers(self, Consumer, channel):
        """Implement providing kombu.Consumers with queues/callbacks."""
        return [
            Consumer(
                queues=self.queue,
                callbacks=[self.on_message],
                accept=[self.message_default_format],
            )
        ]

    def requeue_workflow(self, **kwargs):
        """Send a workflow back to the queue.

        We do not use ``message.requeue()`` because it cannot be used after
        ``message.ack()``, and we cannot wait to validate all the checks
        (``reana_ready()`` and calling RWC) without calling ``message.ack()``
        and getting a new call to on_message with the same workflow.
        """
        try:
            current_workflow_submission_publisher.publish_workflow_submission(
                kwargs["user"], kwargs["workflow_id_or_name"], kwargs["parameters"]
            )
            logging.error(
                f"Requeueing workflow " f'{kwargs["workflow_id_or_name"]} ...'
            )
        except KeyError:
            logging.error(
                f"Wrong parameters to requeue workflow:\n"
                f"{kwargs}\n"
                f"Did reana_commons.publisher.WorkflowSubmissionPublisher's "
                f"method publish_workflow_submission change its signature?",
                exc_info=True,
            )
        except Exception:
            logging.error(
                "An error has occurred while requeueing worfklow", exc_info=True
            )

    def on_message(self, workflow_submission, message):
        """On new workflow_submission event handler."""
        message.ack()
        workflow_submission = json.loads(workflow_submission)
        if reana_ready():
            logging.info("Starting queued workflow: {}".format(workflow_submission))
            workflow_submission["status"] = "start"
            try:
                requeue = True
                started = False
                (
                    response,
                    http_response,
                ) = current_rwc_api_client.api.set_workflow_status(
                    **workflow_submission
                ).result()
                http_response_json = http_response.json()
                if http_response.status_code == 200:
                    started = True
                    logging.info(
                        f"Workflow "
                        f'{http_response_json["workflow_id"]} '
                        f"successfully started."
                    )
                else:
                    logging.error(
                        f"RWC returned an unexpected status code:\n"
                        f"{http_response_json}"
                    )

            except HTTPBadGateway as api_e:
                logging.error(
                    f"Workflow failed to start because "
                    f"RWC got an error while calling an external"
                    f"service (i.e. DB):\n"
                    f"{api_e}",
                    exc_info=True,
                )
            except HTTPNotFound as not_found_e:
                logging.error(
                    f"Workflow failed to start because "
                    f"workflow does not exist or was deleted \n"
                    f"{not_found_e}",
                    exc_info=True,
                )
                requeue = False
            except Exception as e:
                logging.error(
                    f"Something went wrong while calling RWC :\n" f"{e}", exc_info=True
                )
            finally:
                if not started and requeue:
                    self.requeue_workflow(**workflow_submission)
        else:
            logging.info(
                "REANA not ready to run workflow "
                f'{workflow_submission["workflow_id_or_name"]}. '
                "Requeueing workflow and retrying in "
                f"{REANA_SCHEDULER_SECONDS_TO_WAIT_FOR_REANA_READY} second(s) ..."
            )
            self.requeue_workflow(**workflow_submission)
            sleep(REANA_SCHEDULER_SECONDS_TO_WAIT_FOR_REANA_READY)
