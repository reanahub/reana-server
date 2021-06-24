# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Pytest configuration for REANA-Workflow-Controller."""

from __future__ import absolute_import, print_function

import os

import flask_login
import pytest
from mock import Mock, patch
from reana_commons.config import MQ_DEFAULT_QUEUES
from reana_commons.publisher import WorkflowSubmissionPublisher

from reana_server.factory import create_app


@pytest.fixture(scope="module")
def base_app(tmp_shared_volume_path):
    """Flask application fixture."""
    config_mapping = {
        "AVAILABLE_WORKFLOW_ENGINES": "serial",
        "SERVER_NAME": "localhost:5000",
        "SECRET_KEY": "SECRET_KEY",
        "TESTING": True,
        "FLASK_ENV": "development",
        "SHARED_VOLUME_PATH": tmp_shared_volume_path,
        "SQLALCHEMY_DATABASE_URI": os.getenv("REANA_SQLALCHEMY_DATABASE_URI"),
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
    }
    app_ = create_app(config_mapping=config_mapping)
    return app_


@pytest.fixture()
def _get_user_mock():
    mocked_user = Mock(is_authenticated=False, roles=[])
    mocked_get_user = Mock(return_value=mocked_user)
    with patch("flask_login.utils._get_user", mocked_get_user):
        yield flask_login.utils._get_user
