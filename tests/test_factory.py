# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2020, 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test factory app."""

from reana_server.factory import create_minimal_app


def test_create_app():
    """Test create_minimal_app() method."""
    create_minimal_app()
