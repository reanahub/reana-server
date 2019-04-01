# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Main entrypoint for Reana-Server."""

import logging

from flask import current_app

from reana_server.factory import create_app

# Needed for flask.with_appcontext decorator to work.
app = create_app()


@app.teardown_appcontext
def shutdown_session(response_or_exc):
    """Close session on app teardown."""
    current_app.session.remove()
    return response_or_exc


if __name__ == '__main__':
    app.run(host='0.0.0.0')
