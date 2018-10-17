# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test server views."""

import json

import pytest
from flask import url_for
from jsonschema.exceptions import ValidationError
from mock import Mock, PropertyMock, patch
from pytest_reana.fixtures import default_user
from pytest_reana.test_utils import make_mock_api_client


def test_get_workflows(app, default_user):
    """Test get_workflows view."""
    with app.test_client() as client:
        with patch('reana_server.rest.workflows.current_rwc_api_client',
                   make_mock_api_client('reana-workflow-controller')):
            res = client.get(url_for('workflows.get_workflows'),
                             query_string={"user_id":
                                           default_user.id_})
            assert res.status_code == 403

            res = client.get(url_for('workflows.get_workflows'),
                             query_string={"access_token":
                                           default_user.access_token})
            assert res.status_code == 200


def test_create_workflow(app, default_user):
    """Test create_workflow view."""
    with app.test_client() as client:
        with patch('reana_server.rest.workflows.current_rwc_api_client',
                   make_mock_api_client('reana-workflow-controller')):
            res = client.post(url_for('workflows.create_workflow'),
                              query_string={"user_id":
                                            default_user.id_})
            assert res.status_code == 403

            # workflow_data with incorrect spec type
            workflow_data = {'workflow': {'specification': {},
                                          'type': 'serial'},
                             'workflow_name': 'test'}
            res = client.post(url_for('workflows.create_workflow'),
                              headers={'Content-Type': 'application/json'},
                              query_string={"access_token":
                                            default_user.access_token},
                              data=json.dumps(workflow_data))
            assert res.status_code == 200
