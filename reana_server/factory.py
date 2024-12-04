# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021, 2022, 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask-application factory for Reana-Server."""

import logging

from flask import Flask, current_app
from invenio_i18n import Babel, InvenioI18N
from flask_menu import Menu as FlaskMenu
from flask_oauthlib.client import OAuth as FlaskOAuth
from invenio_accounts import InvenioAccounts
from invenio_db import InvenioDB
from invenio_oauthclient import InvenioOAuthClient
from invenio_oauthclient.views.client import blueprint as blueprint_client
from invenio_oauthclient.views.settings import blueprint as blueprint_settings
from reana_commons.config import REANA_LOG_FORMAT, REANA_LOG_LEVEL
from reana_db.database import Session


def create_minimal_app(config_mapping=None):
    """REANA Server application factory.

    Create a minimal Flask app containing all of REANA's endpoints and the needed
    Invenio modules. Use `invenio_app.factory.create_app` the create the full Invenio
    app that is also used in production or when invoking `invenio run`.

    This method is used to create the Flask app in the tests and in the
    `generate_openapi_spec.py` script.

    In general, this is how Flask apps are created:
    - When running in debug mode, `invenio run ...` is invoked. This calls `invenio_app.factory.create_app`.
    - When running in production mode, `uwsgi` is used, and the module configured is `invenio_app.wsgi:application`. This calls `invenio_app.factory.create_app`.
    - When running `flask reana-admin` commands, flask auto-detects the app present in `app.py`, which is created with `invenio_app.factory.create_app`.
    - When running the tests, `reana_server.factory.create_minimal_app` is called.
    - When running `generate_openapi_spec.py`, the app is created with `reana_server.factory.create_minimal_app`.
    """
    logging.basicConfig(level=REANA_LOG_LEVEL, format=REANA_LOG_FORMAT, force=True)
    app = Flask(__name__)
    app.config.from_object("reana_server.config")
    if config_mapping:
        app.config.from_mapping(config_mapping)

    app.session = Session

    # Inspired from https://github.com/inveniosoftware/invenio-accounts/blob/345abfc2d3bf4af0be898a1b4ee1fe45edd16053/tests/conftest.py#L66
    Babel(app)
    FlaskMenu(app)
    InvenioDB(app)
    InvenioI18N(app)
    InvenioAccounts(app)
    FlaskOAuth(app)
    InvenioOAuthClient(app)

    # Register Invenio OAuth endpoints
    app.register_blueprint(blueprint_client)
    app.register_blueprint(blueprint_settings)

    # Register API routes
    from .rest import (
        config,
        gitlab,
        ping,
        secrets,
        status,
        users,
        workflows,
        info,
        launch,
    )  # noqa

    app.register_blueprint(ping.blueprint, url_prefix="/api")
    app.register_blueprint(workflows.blueprint, url_prefix="/api")
    app.register_blueprint(users.blueprint, url_prefix="/api")
    app.register_blueprint(secrets.blueprint, url_prefix="/api")
    app.register_blueprint(gitlab.blueprint, url_prefix="/api")
    app.register_blueprint(config.blueprint, url_prefix="/api")
    app.register_blueprint(status.blueprint, url_prefix="/api")
    app.register_blueprint(info.blueprint, url_prefix="/api")
    app.register_blueprint(launch.blueprint, url_prefix="/api")

    @app.teardown_appcontext
    def shutdown_session(response_or_exc):
        """Close session on app teardown."""
        current_app.session.remove()
        return response_or_exc

    return app
