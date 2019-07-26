# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask extension REANA-Server."""

from flask_menu import Menu

from reana_server import config


class REANA(object):
    """REANA Invenio app."""

    def __init__(self, app=None):
        """Extension initialization."""
        if app:
            self.app = app
            self.init_app(app)

    def init_app(self, app):
        """Flask application initialization."""
        self.init_config(app)
        Menu(app=app)

        @app.before_first_request
        def connect_signals():
            """Connect OAuthClient signals."""
            from invenio_oauthclient.signals import account_info_received

            from .utils import _create_and_associate_reana_user

            account_info_received.connect(
                _create_and_associate_reana_user
            )

    def init_config(self, app):
        """Initialize configuration."""
        for k in dir(config):
            if k.startswith('REANA_'):
                app.config.setdefault(k, getattr(config, k))
