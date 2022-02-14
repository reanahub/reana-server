# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021, 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server Workflow Execution Scheduler."""

import json
import logging
from functools import partial
from time import sleep
from typing import Dict

from bravado.exception import HTTPBadGateway, HTTPNotFound, HTTPConflict
from kubernetes.client.rest import ApiException
from sqlalchemy import func, or_
from sqlalchemy.exc import SQLAlchemyError

from reana_commons.config import REANA_MAX_CONCURRENT_BATCH_WORKFLOWS
from reana_commons.consumer import BaseConsumer
from reana_commons.publisher import WorkflowStatusPublisher
from reana_commons.k8s.api_client import current_k8s_corev1_api_client
from reana_db.database import Session
from reana_db.models import Workflow, RunStatus

from reana_server.api_client import (
    current_rwc_api_client,
    current_workflow_submission_publisher,
)
from reana_server.config import (
    REANA_SCHEDULER_REQUEUE_SLEEP,
    REANA_SCHEDULER_REQUEUE_COUNT,
)
from reana_server.status import NodesStatus


def check_memory_availability(workflow_min_job_memory):
    """Check if at least one workflow job could be started in Kubernetes."""
    nodes = NodesStatus().get_available_memory()
    if not nodes:
        return True
    max_node_available_memory = max(nodes)
    return max_node_available_memory >= workflow_min_job_memory


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
    doesnt_exceed = True
    try:
        running_workflows = (
            Session.query(func.count())
            .filter(
                or_(
                    Workflow.status == RunStatus.pending,
                    Workflow.status == RunStatus.running,
                )
            )
            .scalar()
        )
        if running_workflows >= REANA_MAX_CONCURRENT_BATCH_WORKFLOWS:
            doesnt_exceed = False
    except SQLAlchemyError as e:
        logging.error(
            "Something went wrong while querying for number of running workflows."
        )
        logging.error(e)
        doesnt_exceed = False
    Session.commit()
    return doesnt_exceed


def reana_ready(workflow_min_job_memory):
    """Check if REANA can start new workflows."""
    conditions = [check_predefined_conditions, doesnt_exceed_max_reana_workflow_count]

    # Do not calculate memory availability on fifo strategy
    if workflow_min_job_memory:
        conditions.append(partial(check_memory_availability, workflow_min_job_memory))

    for check_condition in conditions:
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

        self.workflow_status_publisher = WorkflowStatusPublisher(
            connection=self.connection
        )

    def get_consumers(self, Consumer, channel):
        """Implement providing kombu.Consumers with queues/callbacks."""
        return [
            Consumer(
                queues=self.queue,
                callbacks=[self.on_message],
                accept=[self.message_default_format],
                prefetch_count=1,  # receive only one message at a time
            )
        ]

    def _fail_workflow(self, workflow_id: str, logs: str = "") -> None:
        self.workflow_status_publisher.publish_workflow_status(
            workflow_id, status=RunStatus.failed.value, logs=logs,
        )

    def _retry_submission(self, workflow_id: str, workflow_submission: Dict) -> None:
        retry_count = workflow_submission.get("retry_count", 0)
        if retry_count >= REANA_SCHEDULER_REQUEUE_COUNT:
            error_message = (
                f"Workflow {workflow_submission['workflow_id_or_name']} failed to schedule after "
                f"{retry_count} retries. Giving up."
            )
            logging.error(error_message)
            self._fail_workflow(workflow_id, logs=error_message)
        else:
            current_workflow_submission_publisher.publish_workflow_submission(
                user_id=workflow_submission["user"],
                workflow_id_or_name=workflow_submission["workflow_id_or_name"],
                parameters=workflow_submission["parameters"],
                priority=workflow_submission["priority"],
                min_job_memory=workflow_submission["min_job_memory"],
                retry_count=retry_count + 1,
            )

    def on_message(self, body, message):
        """On new workflow_submission event handler."""
        workflow_submission = json.loads(body)
        logging.info(f"Received workflow: {workflow_submission}")

        workflow_submission_copy = workflow_submission.copy()

        workflow_id = workflow_submission["workflow_id_or_name"]
        workflow_min_job_memory = workflow_submission.pop("min_job_memory", 0)

        workflow_submission.pop("priority", None)
        workflow_submission.pop("retry_count", None)

        if reana_ready(workflow_min_job_memory):
            logging.info(f"Starting queued workflow: {workflow_id}")
            workflow_submission["status"] = "start"

            retry = True
            started = False

            try:
                (
                    response,
                    http_response,
                ) = current_rwc_api_client.api.set_workflow_status(
                    **workflow_submission
                ).result()
                http_response_json = http_response.json()
                started = True
                logging.info(
                    f'Workflow {http_response_json["workflow_id"]} successfully started.'
                )

            except HTTPBadGateway as api_e:
                logging.error(
                    "Workflow failed to start because RWC got an error while calling"
                    f"an external service (i.e. DB):\n {api_e}",
                    exc_info=True,
                )
            except HTTPNotFound as not_found_e:
                # if workflow is not found, we cannot retry or report an error to workflow logs
                retry = False
                logging.error(
                    "Workflow failed to start because it does not exist or was deleted \n"
                    f"{not_found_e}",
                    exc_info=True,
                )
            except HTTPConflict as e:
                retry = False
                logging.error(
                    f"Workflow failed to start because of duplicated message from RabbitMQ.\n {e}",
                    exc_info=True,
                )
            except Exception as e:
                logging.error(
                    f"Something went wrong while calling RWC:\n {e}", exc_info=True
                )
            finally:
                sleep(REANA_SCHEDULER_REQUEUE_SLEEP)
                if not started and retry:
                    message.reject()
                    self._retry_submission(workflow_id, workflow_submission_copy)
                else:
                    message.ack()
        else:
            logging.info(
                f'REANA not ready to run workflow {workflow_submission["workflow_id_or_name"]}.'
            )
            sleep(REANA_SCHEDULER_REQUEUE_SLEEP)
            message.reject()
            self._retry_submission(workflow_id, workflow_submission_copy)
