# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask extension REANA-Server."""

import logging

from flask_menu import Menu
from reana_commons.config import REANA_LOG_FORMAT, REANA_LOG_LEVEL

from reana_server import config


class REANA(object):
    """REANA Invenio app."""

    def __init__(self, app=None):
        """Extension initialization."""
        logging.basicConfig(level=REANA_LOG_LEVEL, format=REANA_LOG_FORMAT, force=True)
        werkzeug_logger = logging.getLogger("werkzeug")
        werkzeug_logger.propagate = False
        if app:
            self.app = app
            self.init_app(app)

    def init_app(self, app):
        """Flask application initialization."""
        self.init_config(app)
        Menu(app=app)

        @app.teardown_appcontext
        def shutdown_reana_db_session(response_or_exc):
            """Close session on app teardown."""
            from reana_db.database import Session as reana_db_session
            from invenio_db import db as invenio_db

            reana_db_session.remove()
            invenio_db.session.remove()
            return response_or_exc

        @app.before_first_request
        def connect_signals():
            """Connect OAuthClient signals."""
            from invenio_oauthclient.signals import account_info_received
            from flask_security.signals import user_registered

            from .utils import (
                _create_and_associate_local_user,
                _create_and_associate_oauth_user,
            )

            account_info_received.connect(_create_and_associate_oauth_user)
            user_registered.connect(_create_and_associate_local_user)

    def init_config(self, app):
        """Initialize configuration."""
        for k in dir(config):
            if k.startswith("REANA_"):
                app.config.setdefault(k, getattr(config, k))
