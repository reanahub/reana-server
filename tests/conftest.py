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
import shutil

import pytest
from flask import Flask
from mock import Mock
from reana_commons.api_client import BaseAPIClient
from reana_db.database import Session
from reana_db.models import Base, User
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy_utils import create_database, database_exists, drop_database

from reana_server.factory import create_app


@pytest.fixture(scope='module')
def base_app():
    """Flask application fixture."""
    config_mapping = {
        'AVAILABLE_WORKFLOW_ENGINES': 'serial',
        'SERVER_NAME': 'localhost:5000',
        'SECRET_KEY': 'SECRET_KEY',
        'TESTING': True,
        'SHARED_VOLUME_PATH': '/tmp/test',
        'SQLALCHEMY_DATABASE_URI':
        'sqlite:///',
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
    }
    app = Flask(__name__)
    app.config.from_mapping(config_mapping)
    app.secret_key = "hyper secret key"

    # Register API routes
    from reana_server.rest import ping, workflows, users, secrets  # noqa
    app.register_blueprint(ping.blueprint, url_prefix='/api')
    app.register_blueprint(workflows.blueprint, url_prefix='/api')
    app.register_blueprint(users.blueprint, url_prefix='/api')
    app.register_blueprint(secrets.blueprint, url_prefix='/api')

    app.session = Session
    return app


@pytest.fixture()
def app(base_app, db_engine, session):
    """Flask application fixture."""
    with base_app.app_context():
        import reana_db.models
        Base.metadata.create_all(bind=db_engine)
        yield base_app
        for table in reversed(Base.metadata.sorted_tables):
            db_engine.execute(table.delete())
