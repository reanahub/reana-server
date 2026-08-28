# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Utilities for pruning persisted workflow logs."""

from datetime import datetime
from typing import Iterator

from reana_db.database import Session
from reana_db.models import (
    Job,
    RunStatus,
    ServiceLog,
    Workflow,
    WorkflowService,
)
from sqlalchemy import func, select

LOG_RETENTION_WORKFLOW_STATUSES = (
    RunStatus.finished,
    RunStatus.failed,
    RunStatus.stopped,
    RunStatus.deleted,
)


def iter_log_retention_candidates(
    cutoff: datetime, batch_size: int = 100
) -> Iterator[Workflow]:
    """Yield terminated workflows whose persisted logs have expired.

    Terminal timestamps are preferred. The workflow update or creation time is
    used as a fallback for legacy records for which no terminal timestamp was
    recorded.
    """
    terminal_at = func.coalesce(
        Workflow.run_finished_at,
        Workflow.run_stopped_at,
        Workflow.updated,
        Workflow.created,
    )
    last_workflow_id = None

    while True:
        query = Session.query(Workflow).filter(
            Workflow.status.in_(LOG_RETENTION_WORKFLOW_STATUSES),
            Workflow.logs_pruned_at.is_(None),
            terminal_at <= cutoff,
        )
        if last_workflow_id is not None:
            query = query.filter(Workflow.id_ > last_workflow_id)

        workflows = query.order_by(Workflow.id_).limit(batch_size).all()
        if not workflows:
            return

        for workflow in workflows:
            yield workflow

        last_workflow_id = workflows[-1].id_


def prune_workflow_logs(workflow: Workflow, pruned_at: datetime) -> None:
    """Remove all database-backed logs of a workflow and stamp the audit time.

    The caller owns the transaction and must commit only after this function
    succeeds.
    """
    Session.query(Job).filter(Job.workflow_uuid == workflow.id_).update(
        {Job.logs: None}, synchronize_session=False
    )

    service_ids = select(WorkflowService.service_id).where(
        WorkflowService.workflow_id == workflow.id_
    )
    Session.query(ServiceLog).filter(ServiceLog.service_id.in_(service_ids)).delete(
        synchronize_session="fetch"
    )

    workflow.logs = None
    workflow.logs_pruned_at = pruned_at
