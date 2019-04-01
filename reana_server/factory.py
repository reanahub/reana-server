# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask-application factory for Reana-Server."""

import logging

from flask import Flask
from flask_cors import CORS
from reana_commons.config import REANA_LOG_FORMAT, REANA_LOG_LEVEL
from reana_db.database import Session


def create_app():
    """REANA Server application factory."""
    logging.basicConfig(
        level=REANA_LOG_LEVEL,
        format=REANA_LOG_FORMAT
    )
    app = Flask(__name__)
    app.config.from_object('reana_server.config')
    app.secret_key = "hyper secret key"

    # Register API routes
    from .rest import ping, workflows, users  # noqa
    app.register_blueprint(ping.blueprint, url_prefix='/api')
    app.register_blueprint(workflows.blueprint, url_prefix='/api')
    app.register_blueprint(users.blueprint, url_prefix='/api')

    app.session = Session
    CORS(app)
    return app
