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
# You should have received a copy of the GNU General Public License
# along with REANA; if not, see <http://www.gnu.org/licenses>.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization or
# submit itself to any jurisdiction.

"""Flask application configuration."""

import os

COMPONENTS_DATA = {
    'reana-workflow-controller': (
        'http://{address}:{port}'.format(
            address=os.getenv('WORKFLOW_CONTROLLER_SERVICE_HOST', '0.0.0.0'),
            port=os.getenv('WORKFLOW_CONTROLLER_SERVICE_PORT_HTTP', '5000')),
        'reana_workflow_controller.json')
}
"""REANA Workflow Controller address."""

AVAILABLE_WORKFLOW_ENGINES = [
    'yadage',
]
"""Available workflow engines."""
