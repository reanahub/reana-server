# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""FastAPI routers for REANA-Server.

MVP slice of the Flask ``reana_server/rest`` blueprints, ported to
``APIRouter`` + Pydantic + the native auth dependencies. The remaining
blueprints (secrets, gitlab, launch, quota, status, info, config, the full
workflows surface) follow the same pattern.
"""
