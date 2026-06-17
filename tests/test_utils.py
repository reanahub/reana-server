# This file is part of REANA.
# Copyright (C) 2021, 2022, 2023, 2024, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server tests for utils module."""

from datetime import datetime
import pathlib

import pytest
from reana_commons.errors import REANAValidationError
from reana_db.models import ResourceType, UserToken, UserTokenStatus, UserTokenType
from reana_server.utils import (
    _set_quota_period,
    filter_input_files,
    get_user_from_token,
    is_valid_email,
)


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


def test_get_user_from_token(user0):
    """Test getting user from his own token."""
    assert user0.id_ == get_user_from_token(user0.access_token).id_


def test_get_user_from_token_after_revocation(user0, session):
    """Test getting user from revoked token."""
    token = user0.active_token
    token.status = UserTokenStatus.revoked
    session.commit()
    with pytest.raises(ValueError, match="revoked"):
        get_user_from_token(token.token)


def test_get_user_from_token_two_tokens(user0, session):
    """Test getting user with multiple tokens."""
    old_token = user0.active_token
    old_token.status = UserTokenStatus.revoked
    new_token = UserToken(
        token="new_token",
        user_id=user0.id_,
        type_=UserTokenType.reana,
        status=UserTokenStatus.active,
    )
    session.add(new_token)
    session.commit()

    # Check that new token works
    assert user0.id_ == get_user_from_token(new_token.token).id_
    # Check that old revoked token does not work
    with pytest.raises(ValueError, match="revoked"):
        get_user_from_token(old_token.token)


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
