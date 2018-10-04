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

from flask import url_for
from mock import Mock, PropertyMock, patch
from jsonschema.exceptions import ValidationError
from reana_commons.test_utils import make_mock_api_client

from reana_server.config import COMPONENTS_DATA


def test_get_workflows(app, default_user):
    """Test get_workflows view."""
    with app.test_client() as client:
        with patch('reana_server.rest.workflows.current_rwc_api_client',
                   make_mock_api_client(
                       'reana_server',
                       COMPONENTS_DATA['reana-workflow-controller'])):
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
                   make_mock_api_client(
                       'reana_server',
                       COMPONENTS_DATA['reana-workflow-controller'])):
            res = client.post(url_for('workflows.create_workflow'),
                              query_string={"user_id":
                                            default_user.id_})
            assert res.status_code == 403

            # workflow_data with incorrect spec type
            workflow_data = {'workflow': {'spec': 0,
                                          'type': 'serial'},
                             'workflow_name': 'test'}
            res = client.post(url_for('workflows.create_workflow'),
                              headers={'Content-Type': 'application/json'},
                              query_string={"access_token":
                                            default_user.access_token},
                              data=json.dumps(workflow_data))
            assert res.status_code == 500

            # corrected workflow_data
            workflow_data['workflow']['spec'] = {}
            res = client.post(url_for('workflows.create_workflow'),
                              headers={'Content-Type': 'application/json'},
                              query_string={"access_token":
                                            default_user.access_token},
                              data=json.dumps(workflow_data))
            assert res.status_code == 200
