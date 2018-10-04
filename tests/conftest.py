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
