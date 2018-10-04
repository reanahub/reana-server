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
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy_utils import create_database, database_exists, drop_database

from reana_commons.api_client import BaseAPIClient
from reana_db.database import Session
from reana_db.models import Base, User
from reana_server.config import COMPONENTS_DATA
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
        'COMPONENTS_DATA': COMPONENTS_DATA,
    }
    app = Flask(__name__)
    app.config.from_mapping(config_mapping)
    app.secret_key = "hyper secret key"

    # Register API routes
    from reana_server.rest import ping, workflows, users  # noqa
    app.register_blueprint(ping.blueprint, url_prefix='/api')
    app.register_blueprint(workflows.blueprint, url_prefix='/api')
    app.register_blueprint(users.blueprint, url_prefix='/api')

    app.session = Session
    return app


@pytest.fixture(scope='module')
def db_engine(base_app):
    test_db_engine = create_engine(
        base_app.config['SQLALCHEMY_DATABASE_URI'])
    if not database_exists(test_db_engine.url):
        create_database(test_db_engine.url)
    yield test_db_engine
    drop_database(test_db_engine.url)


@pytest.fixture()
def session(db_engine):
    Session = scoped_session(sessionmaker(autocommit=False,
                                          autoflush=False,
                                          bind=db_engine))
    Base.query = Session.query_property()
    from reana_db.database import Session as _Session
    _Session.configure(bind=db_engine)
    yield Session


@pytest.fixture()
def app(base_app, db_engine, session):
    """Flask application fixture."""
    with base_app.app_context():
        import reana_db.models
        Base.metadata.create_all(bind=db_engine)
        yield base_app
        for table in reversed(Base.metadata.sorted_tables):
            db_engine.execute(table.delete())


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
def mock_rwc_api_client():
    mock_http_client, mock_result, mock_response = Mock(), Mock(), Mock()
    mock_response.status_code = 200
    mock_result.result.return_value = ('_', mock_response)
    mock_http_client.request.return_value = mock_result
    mock_rwc_api_client = BaseAPIClient('reana_server',
                                        'reana-workflow-controller',
                                        http_client=mock_http_client)
    return mock_rwc_api_client._client
