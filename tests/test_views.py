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
from jsonschema.exceptions import ValidationError
from mock import MagicMock, Mock
from reana_db.models import User

from reana_server.api_client import create_openapi_client, get_spec
from reana_server.config import COMPONENTS_DATA


@pytest.fixture()
def default_user(app, session):
    """Create users."""
    default_user_id = '00000000-0000-0000-0000-000000000000'
    user = User.query.filter_by(
        id_=default_user_id).first()
    if not user:
        user = User(id_=default_user_id,
                    email='info@reana.io', access_token='secretkey')
        session.add(user)
        session.commit()
    return user


@pytest.fixture()
def mock_rwc_client(app):
    """."""
    mock_rwc_client = create_openapi_client('reana-workflow-controller',
                                            Mock())
    return mock_rwc_client


def test_swagger_stub(app, default_user, mock_rwc_client):
    with pytest.raises(ValidationError):
        res = mock_rwc_client.api.create_workflow(
            workflow={'specification': {},
                      # 'type': 'serial',
                      'name': 'test'},
            user=str(default_user.id_)).result()

    res = mock_rwc_client.api.create_workflow(
        workflow={'specification': {},
                  'type': 'serial',
                  'name': 'test'},
        user=str(default_user.id_)).result()


def test_prism_server(app, default_user):
    """."""
    rwc_api_client = create_openapi_client('reana-workflow-controller')
    with pytest.raises(ValidationError):
        res = rwc_api_client.api.create_workflow(
            workflow={'specification': {},
                      # 'type': 'serial',
                      'name': 'test'},
            user=str(default_user.id_)).result()

    _, res = rwc_api_client.api.create_workflow(
        workflow={'specification': {},
                  'type': 'serial',
                  'name': 'test'},
        user=str(default_user.id_)).result()
    assert res.status_code == 201
