# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2020, 2021, 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test factory app."""

from reana_server.factory import create_minimal_app
from reana_server.rest.notifications import blueprint as notifications_blueprint


def test_create_app():
    """Test create_minimal_app() method."""
    create_minimal_app()


def test_notifications_blueprint_is_importable():
    """Ensure the notifications entry point target is packaged."""
    assert notifications_blueprint.name == "notifications"
