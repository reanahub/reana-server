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
from typing import Dict, Optional

from bravado.exception import HTTPBadGateway, HTTPNotFound, HTTPConflict, HTTPBadRequest
from sqlalchemy import func, or_
from sqlalchemy.exc import SQLAlchemyError

from reana_commons.config import REANA_MAX_CONCURRENT_BATCH_WORKFLOWS
from reana_commons.consumer import BaseConsumer
from reana_commons.publisher import WorkflowStatusPublisher
from reana_db.database import Session
from reana_db.models import Workflow, RunStatus

from reana_server.api_client import (
    current_rwc_api_client,
    current_workflow_submission_publisher,
)
from reana_server.config import (
    REANA_SCHEDULER_REQUEUE_SLEEP,
    REANA_SCHEDULER_REQUEUE_COUNT,
    REANA_WORKFLOW_SCHEDULING_POLICY,
    REANA_WORKFLOW_SCHEDULING_READINESS_CHECK_VALUE,
)
from reana_server.status import NodesStatus


def check_memory_availability(
    workflow_min_job_memory: Optional[float],
) -> Optional[str]:
    """Check if at least one workflow job could be started in Kubernetes."""
    error = None
    # Do not calculate memory availability on fifo strategy
    if not workflow_min_job_memory:
        return error
    nodes = NodesStatus().get_available_memory()
    if not nodes:
        logging.info(
            "Information about nodes memory is not available. Skipping memory check.."
        )
        return error
    max_node_available_memory = max(nodes)
    available = max_node_available_memory >= workflow_min_job_memory
    if not available:
        error = (
            f"workflow requires {workflow_min_job_memory} memory whilst "
            f"only {max_node_available_memory} is available."
        )
    return error


def check_concurrent_workflows_limit() -> Optional[str]:
    """Check upper limit on running REANA batch workflows."""
    error = None
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
            error = (
                f"there are already {running_workflows} running workflows "
                f"whilst the allowed maximum is {REANA_MAX_CONCURRENT_BATCH_WORKFLOWS}."
            )
    except SQLAlchemyError as e:
        error = "Something went wrong while querying for number of running workflows."
        logging.error(error)
        logging.error(e)
    Session.commit()
    return error


def reana_ready(workflow_min_job_memory: Optional[float]) -> Optional[str]:
    """Check if REANA can start new workflows."""
    check_memory = partial(check_memory_availability, workflow_min_job_memory)

    conditions_map = {
        "no_checks": [],
        "concurrent": [check_concurrent_workflows_limit],
        "memory": [check_memory],
        "all_checks": [check_concurrent_workflows_limit, check_memory],
    }
    conditions = conditions_map.get(REANA_WORKFLOW_SCHEDULING_READINESS_CHECK_VALUE)

    for check_condition in conditions:
        error = check_condition()
        if error:
            return error
    return None


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
        logging.info(
            "WorkflowExecutionScheduler initialized with the following settings: \n"
            f"- REANA_SCHEDULER_REQUEUE_SLEEP: {REANA_SCHEDULER_REQUEUE_SLEEP}\n"
            f"- REANA_SCHEDULER_REQUEUE_COUNT: {REANA_SCHEDULER_REQUEUE_COUNT}\n"
            f"- REANA_WORKFLOW_SCHEDULING_POLICY: {REANA_WORKFLOW_SCHEDULING_POLICY}\n"
            f"- REANA_WORKFLOW_SCHEDULING_READINESS_CHECK_VALUE: {REANA_WORKFLOW_SCHEDULING_READINESS_CHECK_VALUE}"
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
            workflow_id,
            status=RunStatus.failed.value,
            logs=logs,
        )

    def _retry_submission(
        self, workflow_id: str, workflow_submission: Dict, reason: Optional[str] = None
    ) -> None:
        retry_count = workflow_submission.get("retry_count", 0)

        if retry_count >= REANA_SCHEDULER_REQUEUE_COUNT:
            error_message = (
                f"Workflow {workflow_submission['workflow_id_or_name']} failed to schedule after "
                f"{retry_count} retries. Giving up."
            )
            if reason:
                error_message += f"\nReason: {reason}"
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

        error = reana_ready(workflow_min_job_memory)
        if not error:
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

            except HTTPBadGateway:
                error = (
                    "Workflow failed to start because reana-workflow-controller got an "
                    "error while calling an external service (i.e. database)."
                )
                logging.exception(error)
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
            except HTTPBadRequest as e:
                retry = False
                try:
                    error_message = e.response.json()["message"]
                except Exception:
                    error_message = str(e)
                logging.error(
                    f"Workflow failed to start because of a bad request.\n{error_message}",
                    exc_info=True,
                )
                self._fail_workflow(workflow_id, logs=error_message)
            except Exception:
                error = "Something went wrong while calling reana-workflow-controller."
                logging.exception(error)
            finally:
                sleep(REANA_SCHEDULER_REQUEUE_SLEEP)
                if not started and retry:
                    message.reject()
                    self._retry_submission(workflow_id, workflow_submission_copy, error)
                else:
                    message.ack()
        else:
            logging.info(
                f'REANA not ready to run workflow {workflow_submission["workflow_id_or_name"]}. '
                f"Reason: {error}"
            )
            sleep(REANA_SCHEDULER_REQUEUE_SLEEP)
            message.reject()
            self._retry_submission(workflow_id, workflow_submission_copy, error)
