# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021, 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Main entrypoint for REANA-Server."""

from invenio_app.factory import create_app

# Needed for flask.with_appcontext decorator to work.
#
# Note that this is the full Flask app including all the necessary Invenio modules.
# See `factory.py` for more details.
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0")
