# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017 CERN.
#
# REANA is free software; you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# REANA is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# REANA; if not, write to the Free Software Foundation, Inc., 59 Temple Place,
# Suite 330, Boston, MA 02111-1307, USA.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization or
# submit itself to any jurisdiction.

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
