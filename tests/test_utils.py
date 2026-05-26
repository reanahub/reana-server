# This file is part of REANA.
# Copyright (C) 2021, 2022, 2023, 2024, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server tests for utils module."""

from datetime import datetime
import os
import pathlib
import stat

import pytest
from mock import patch
from reana_commons.config import REANA_WORKFLOW_UMASK
from reana_commons.errors import REANAValidationError
from reana_db.models import ResourceType, Service, ServiceStatus
from reana_server.utils import (
    _set_quota_period,
    create_user_workspace,
    ensure_dask_service,
    filter_input_files,
    initialise_workspace_umask,
    is_valid_email,
)


@patch("reana_server.utils.os.umask")
def test_initialise_workspace_umask(mock_umask):
    """Set the shared workspace umask on process initialisation."""
    initialise_workspace_umask()

    mock_umask.assert_called_once_with(REANA_WORKFLOW_UMASK)


def test_create_user_workspace_uses_initialised_umask(tmp_path):
    """Create group-writable workspace directories after initialisation."""
    workspace = tmp_path / "user-workspace"
    previous_umask = os.umask(0o022)
    try:
        initialise_workspace_umask()
        create_user_workspace(workspace)
    finally:
        os.umask(previous_umask)

    mode = stat.S_IMODE(workspace.stat().st_mode)
    assert mode == 0o775


@pytest.mark.parametrize(
    "email,is_valid",
    [
        ("john@example.org", True),
        ("john.doe@example.org", True),
        ("john-doe@example.org", True),
        ("john.doe@edu.uni.org", True),
        ("jean-yves.le.meur@cern.ch", True),
        ("john.doe@exampleorg", False),
        ("john.doeexample.org", False),
        ("john@example.org.", False),
        ("john@example..org", False),
        ("john@@example.org", False),
    ],
)
def test_is_email_valid(email: str, is_valid: bool):
    assert is_valid_email(email) == is_valid


def test_filter_input_files(tmp_path: pathlib.Path):
    all_files = ["x/y/z/a.txt", "x/y/b.txt", "x/w/c.txt"]
    for file in all_files:
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Content of {file}")

    reana_yaml = {"inputs": {"directories": ["x/y/z"], "files": ["x/w/c.txt"]}}
    filter_input_files(str(tmp_path), reana_yaml)

    assert (tmp_path / "x/y/z/a.txt").exists()
    assert (tmp_path / "x/w/c.txt").exists()
    assert not (tmp_path / "x/y/b.txt").exists()
    assert len(list(tmp_path.iterdir())) == 1


def test_set_quota_period_rejects_period_start_without_cadence(user0, session):
    """Test setting a period start requires an existing periodic cadence."""
    cpu_user_resource = next(
        resource
        for resource in user0.resources
        if resource.resource.type_ == ResourceType.cpu
    )
    cpu_user_resource.quota_period_months = None
    cpu_user_resource.quota_period_start_at = None
    session.commit()

    msg, status_code, fatal = _set_quota_period(
        resource_type=ResourceType.cpu.name,
        email=user0.email,
        quota_period_start_at=datetime(2026, 7, 1, 0, 0, 0),
    )

    session.refresh(cpu_user_resource)
    assert status_code == 400
    assert fatal is True
    assert "quota_period_months" in msg
    assert cpu_user_resource.quota_period_months is None
    assert cpu_user_resource.quota_period_start_at is None


def test_set_quota_period_accepts_period_start_with_existing_cadence(user0, session):
    """Test setting a period start works once the user has a cadence."""
    cpu_user_resource = next(
        resource
        for resource in user0.resources
        if resource.resource.type_ == ResourceType.cpu
    )
    cpu_user_resource.quota_period_months = 3
    cpu_user_resource.quota_period_start_at = None
    session.commit()

    new_period_start = datetime(2026, 7, 1, 0, 0, 0)
    msg, status_code, fatal = _set_quota_period(
        resource_type=ResourceType.cpu.name,
        email=user0.email,
        quota_period_start_at=new_period_start,
    )

    session.refresh(cpu_user_resource)
    assert msg is None
    assert status_code == 200
    assert fatal is False
    assert cpu_user_resource.quota_period_months == 3
    assert cpu_user_resource.quota_period_start_at == new_period_start


def test_set_quota_period_disabling_cadence_clears_period_start(user0, session):
    """Test disabling periodic quota cadence clears the stored period start."""
    cpu_user_resource = next(
        resource
        for resource in user0.resources
        if resource.resource.type_ == ResourceType.cpu
    )
    cpu_user_resource.quota_period_months = 3
    cpu_user_resource.quota_period_start_at = datetime(2026, 7, 1, 0, 0, 0)
    session.commit()

    msg, status_code, fatal = _set_quota_period(
        resource_type=ResourceType.cpu.name,
        email=user0.email,
        quota_period_months=None,
    )

    session.refresh(cpu_user_resource)
    assert msg is None
    assert status_code == 200
    assert fatal is False
    assert cpu_user_resource.quota_period_months is None
    assert cpu_user_resource.quota_period_start_at is None


def _with_dask(reana_specification, dask):
    workflow = dict(reana_specification.get("workflow", {}))
    resources = {"dask": {"image": "dask:latest"}} if dask else {}
    workflow["resources"] = resources
    return {**reana_specification, "workflow": workflow}


def test_ensure_dask_service_follows_specification(
    session, sample_yadage_workflow_in_db
):
    """The Dask service row is added and removed with the Dask request."""
    workflow = sample_yadage_workflow_in_db
    specification = workflow.reana_specification

    workflow.reana_specification = _with_dask(specification, dask=True)
    assert ensure_dask_service(workflow) is True
    session.commit()
    assert len(workflow.services) == 1
    assert ensure_dask_service(workflow) is False

    # Dask removed from reana.yaml before the workflow is started
    workflow.reana_specification = _with_dask(specification, dask=False)
    assert ensure_dask_service(workflow) is True
    session.commit()
    assert workflow.services == []
    assert session.query(Service).count() == 0
    assert ensure_dask_service(workflow) is False


def test_ensure_dask_service_keeps_started_service(
    session, sample_yadage_workflow_in_db
):
    """A Dask service that is no longer in the created state is kept."""
    workflow = sample_yadage_workflow_in_db
    specification = workflow.reana_specification

    workflow.reana_specification = _with_dask(specification, dask=True)
    ensure_dask_service(workflow)
    workflow.services[0].status = ServiceStatus.running
    session.commit()

    workflow.reana_specification = _with_dask(specification, dask=False)
    assert ensure_dask_service(workflow) is False
    assert len(workflow.services) == 1
