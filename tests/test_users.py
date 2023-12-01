# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2023 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test users endpoints."""


import pytest
from flask import url_for
from mock import patch
from pytest_reana.test_utils import make_mock_api_client


def test_get_users_shared_with_you(app, user1):
    """Test getting users who shared workflows with you."""
    with app.test_client() as client:
        response = client.get(
            url_for("users.get_users_shared_with_you"),
        )

        assert response.status_code == 401

        response = client.get(
            url_for("users.get_users_shared_with_you"),
            query_string={"access_token": "invalid_token"},
        )

        assert response.status_code == 403

        response = client.get(
            url_for("users.get_users_shared_with_you"),
            query_string={"access_token": user1.access_token},
        )

        assert response.status_code == 200


def test_get_users_you_shared_with(app, user1):
    """Test getting users who you shared workflows with."""
    with app.test_client() as client:
        response = client.get(
            url_for("users.get_users_you_shared_with"),
        )

        assert response.status_code == 401

        response = client.get(
            url_for("users.get_users_you_shared_with"),
            query_string={"access_token": "invalid_token"},
        )

        assert response.status_code == 403

        response = client.get(
            url_for("users.get_users_you_shared_with"),
            query_string={"access_token": user1.access_token},
        )

        assert response.status_code == 200
