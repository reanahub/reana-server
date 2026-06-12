# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""WSGI entrypoint for REANA-Server (uwsgi)."""

from reana_server.factory import create_app

application = create_app()
