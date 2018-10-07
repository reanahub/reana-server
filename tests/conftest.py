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
from reana_db.models import Base, User
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy_utils import create_database, database_exists, drop_database

from reana_server.factory import create_app


@pytest.fixture(scope='module')
def tmp_shared_volume_path(tmpdir_factory):
    """Fixture temporary file system database."""
    temp_path = str(tmpdir_factory.mktemp('data').join('reana'))
    shutil.copytree(os.path.join(os.path.dirname(__file__), "data"),
                    temp_path)

    yield temp_path
    shutil.rmtree(temp_path)


@pytest.fixture(scope='module')
def base_app(tmp_shared_volume_path):
    """Flask application fixture."""
    config_mapping = {
        'SERVER_NAME': 'localhost:5000',
        'SECRET_KEY': 'SECRET_KEY',
        'TESTING': True,
        'SHARED_VOLUME_PATH': tmp_shared_volume_path,
        'SQLALCHEMY_DATABASE_URI':
        'sqlite:///',
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
    }
    app_ = create_app(config_mapping)
    return app_


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
