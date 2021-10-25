# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Main entrypoint for Reana-Server."""

from reana_server.factory import create_app

# Needed for flask.with_appcontext decorator to work.
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0")
