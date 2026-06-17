# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021, 2022, 2024, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Minimal Flask CLI-application factory for Reana-Server.

The HTTP API is served by FastAPI (``reana_server.asgi``). This Flask app
exists only to provide an application context + reana-db session for the
Click commands registered as entry points (``flask start-scheduler``,
``flask reana-admin ...``); it registers no web routes.
"""

import logging

from flask import Flask, current_app
from reana_commons.config import REANA_LOG_FORMAT, REANA_LOG_LEVEL
from reana_db.database import Session


def _validate_secret_key(app):
    """Refuse to start without a strong session secret."""
    if not app.config.get("SECRET_KEY"):
        raise ValueError(
            "SECRET_KEY is unset. Provide a strong random value via "
            "secrets.reana.REANA_SECRET_KEY in your Helm values, e.g. "
            "`--set secrets.reana.REANA_SECRET_KEY=$(openssl rand -hex 32)`."
        )


def create_app(config_mapping=None):
    """Build the minimal Flask app used by the REANA CLI commands."""
    logging.basicConfig(level=REANA_LOG_LEVEL, format=REANA_LOG_FORMAT, force=True)
    logging.getLogger("werkzeug").propagate = False

    app = Flask(__name__)
    app.config.from_object("reana_server.config")
    if config_mapping:
        app.config.from_mapping(config_mapping)
    _validate_secret_key(app)
    app.session = Session

    @app.teardown_appcontext
    def shutdown_session(response_or_exc):
        """Close the reana-db session on app teardown."""
        current_app.session.remove()
        return response_or_exc

    return app
