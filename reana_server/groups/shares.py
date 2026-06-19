# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Group workflow share grants.

Group shares (``group_workflow`` rows) are managed entirely in
reana-server: grants reference provider-tagged ``external_group`` rows and
are validated against the group backend at share time. Authorization at
request time never touches a backend — it reads the reana-db snapshot
(see ``reana_db.utils.active_workflow_share_criterion``).
"""

from datetime import datetime

from reana_db.database import Session
from reana_db.models import ExternalGroup, GroupWorkflow

from reana_server.groups import get_group_backend
from reana_server.groups.base import GroupBackendError


class GroupShareValidationError(Exception):
    """Invalid group share request (HTTP 400)."""


class GroupNotFoundError(Exception):
    """The group or grant does not exist (HTTP 404)."""


class GroupShareConflictError(Exception):
    """The workflow is already shared with the group (HTTP 409)."""


class GroupBackendUnavailableError(Exception):
    """The group backend cannot be reached (HTTP 503)."""


def parse_valid_until(value):
    """Parse and validate a ``YYYY-MM-DD`` expiration date."""
    if value is None:
        return None
    try:
        valid_until = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise GroupShareValidationError(
            "Field 'valid_until' must be a date in the format YYYY-MM-DD."
        )
    if valid_until.date() < datetime.utcnow().date():
        raise GroupShareValidationError(
            "Field 'valid_until' cannot be a date in the past."
        )
    return valid_until


def _get_external_group(provider, external_id):
    return (
        Session.query(ExternalGroup)
        .filter_by(provider=provider, external_id=external_id)
        .one_or_none()
    )


def share_workflow_with_group(
    workflow, provider, external_id, message=None, valid_until=None
):
    """Create a read-only group share grant for a workflow.

    The group is validated live against its backend before the grant is
    persisted, so shares can never be created against unknown groups.

    :param workflow: the workflow (caller must have verified ownership).
    :param provider: group backend provider tag (e.g. ``keycloak``).
    :param external_id: the group's immutable identifier.
    :param message: optional message shown to group members.
    :param valid_until: optional ``datetime`` expiration
        (see :func:`parse_valid_until`).
    """
    backend = get_group_backend(provider)
    if backend is None:
        raise GroupShareValidationError(
            f"Unknown group provider '{provider}'."
        )
    try:
        exists = backend.group_exists(external_id)
    except GroupBackendError as error:
        raise GroupBackendUnavailableError(
            f"Group backend '{provider}' is currently unavailable: {error}"
        )
    if not exists:
        raise GroupNotFoundError(
            f"Group '{external_id}' does not exist in provider '{provider}'."
        )
    try:
        group = _get_external_group(provider, external_id)
        if group is None:
            group = ExternalGroup(
                provider=provider,
                external_id=external_id,
                display_name=external_id.rsplit("/", 1)[-1] or external_id,
                last_seen_at=datetime.utcnow(),
            )
            Session.add(group)
            Session.flush()
        existing_grant = (
            Session.query(GroupWorkflow)
            .filter_by(workflow_id=workflow.id_, group_id=group.id_)
            .one_or_none()
        )
        if existing_grant:
            raise GroupShareConflictError(
                "The workflow is already shared with the group."
            )
        Session.add(
            GroupWorkflow(
                workflow_id=workflow.id_,
                group_id=group.id_,
                message=message,
                valid_until=valid_until,
            )
        )
        Session.commit()
    except (GroupShareConflictError, GroupNotFoundError):
        Session.rollback()
        raise
    except Exception:
        Session.rollback()
        raise


def unshare_workflow_with_group(workflow, provider, external_id):
    """Remove a group share grant from a workflow."""
    group = _get_external_group(provider, external_id)
    grant = (
        Session.query(GroupWorkflow)
        .filter_by(workflow_id=workflow.id_, group_id=group.id_)
        .one_or_none()
        if group
        else None
    )
    if grant is None:
        raise GroupNotFoundError(
            f"The workflow is not shared with group '{external_id}' "
            f"(provider '{provider}')."
        )
    try:
        Session.delete(grant)
        Session.commit()
    except Exception:
        Session.rollback()
        raise


def get_group_shares_for_workflow(workflow):
    """Return the workflow's group share grants for share-status views."""
    rows = (
        Session.query(GroupWorkflow, ExternalGroup)
        .join(ExternalGroup, GroupWorkflow.group_id == ExternalGroup.id_)
        .filter(GroupWorkflow.workflow_id == workflow.id_)
        .all()
    )
    return [
        {
            "provider": group.provider,
            "external_id": group.external_id,
            "display_name": group.display_name,
            "message": grant.message,
            "valid_until": (
                grant.valid_until.strftime("%Y-%m-%dT%H:%M:%S")
                if grant.valid_until
                else None
            ),
        }
        for grant, group in rows
    ]
