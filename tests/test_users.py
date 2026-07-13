# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2023, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test users endpoints."""

from datetime import datetime
import json
from pathlib import Path

import pytest
from bravado_core.spec import Spec
from bravado_core.validate import validate_object
from flask import url_for


@pytest.mark.parametrize("full_name", [None, "", "Jane Doe"])
def test_get_you_full_name_matches_schema(app, session, user1, auth_headers, full_name):
    """Optional profile names must not invalidate authenticated user responses."""
    user1.full_name = full_name
    session.commit()

    with app.test_client() as client:
        response = client.get(
            url_for("users.get_you"),
            headers=auth_headers(user1),
        )

    assert response.status_code == 200
    spec_path = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    spec_dict = json.loads(spec_path.read_text())
    schema = spec_dict["paths"]["/api/you"]["get"]["responses"]["200"]["schema"]
    validate_object(Spec.from_dict(spec_dict), schema, response.json)
    assert response.json["full_name"] == (full_name or "")
    assert response.json["email"] == user1.email


def test_get_users_shared_with_you(app, user1, auth_headers):
    """Test getting users who shared workflows with you."""
    with app.test_client() as client:
        response = client.get(
            url_for("users.get_users_shared_with_you"),
        )

        assert response.status_code == 401

        response = client.get(
            url_for("users.get_users_shared_with_you"),
            headers={"Authorization": "Bearer invalid_token"},
        )

        assert response.status_code == 401

        response = client.get(
            url_for("users.get_users_shared_with_you"),
            headers=auth_headers(user1),
        )

        assert response.status_code == 200


def test_get_users_you_shared_with(app, user1, auth_headers):
    """Test getting users who you shared workflows with."""
    with app.test_client() as client:
        response = client.get(
            url_for("users.get_users_you_shared_with"),
        )

        assert response.status_code == 401

        response = client.get(
            url_for("users.get_users_you_shared_with"),
            headers={"Authorization": "Bearer invalid_token"},
        )

        assert response.status_code == 401

        response = client.get(
            url_for("users.get_users_you_shared_with"),
            headers=auth_headers(user1),
        )

        assert response.status_code == 200


def test_get_you_includes_periodic_cpu_quota_metadata(
    app, session, user1, auth_headers
):
    """Test authenticated user info includes periodic CPU quota metadata."""
    cpu_resource = next(
        resource
        for resource in user1.resources
        if resource.resource.type_.name == "cpu"
    )
    cpu_resource.quota_period_months = 3
    cpu_resource.quota_period_start_at = datetime(2026, 4, 1, 13, 6, 32, 992595)
    session.commit()

    with app.test_client() as client:
        response = client.get(
            url_for("users.get_you"),
            headers=auth_headers(user1),
        )

    assert response.status_code == 200
    assert response.json["quota"]["cpu"]["quota_period_months"] == 3
    assert (
        response.json["quota"]["cpu"]["quota_period_start_at"]
        == "2026-04-01T13:06:32.992595Z"
    )
