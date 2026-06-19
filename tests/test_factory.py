# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2020, 2021, 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test factory app."""

import pytest

from reana_server.factory import create_app


def test_create_app():
    """Test create_app() method."""
    create_app(config_mapping={"SECRET_KEY": "test-secret"})


def test_create_app_requires_secret_key():
    """The factory refuses to start without a session secret."""
    with pytest.raises(ValueError, match="SECRET_KEY"):
        create_app(config_mapping={"SECRET_KEY": ""})
