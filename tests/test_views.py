# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test server views."""

import json

from flask import url_for
from jsonschema.exceptions import ValidationError


def test_get_workflows(app, default_user):
    """Test get_workflows view."""
    with app.test_client() as client:
        res = client.get(url_for('workflows.get_workflows'),
                         query_string={"user_id":
                                       default_user.id_})
        assert res.status_code == 403

        res = client.get(url_for('workflows.get_workflows'),
                         query_string={"access_token":
                                       default_user.access_token})
        assert res.status_code == 200
