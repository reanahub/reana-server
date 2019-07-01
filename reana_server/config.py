# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Flask application configuration."""

import os

AVAILABLE_WORKFLOW_ENGINES = [
    'yadage',
    'cwl',
    'serial'
]
"""Available workflow engines."""

ADMIN_USER_ID = "00000000-0000-0000-0000-000000000000"

SHARED_VOLUME_PATH = os.getenv('SHARED_VOLUME_PATH', '/var/reana')

JWT_SECRET_KEY = 'hyper secret key'

JWT_TOKEN_LOCATION = ['cookies']

JWT_COOKIE_SECURE = False

JWT_ACCESS_COOKIE_PATH = '/'

JWT_COOKIE_CSRF_PROTECT = True
