# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REST API client generator."""

import json
import os

import pkg_resources
from flask import current_app
from reana_commons.api_client import BaseAPIClient
from werkzeug.local import LocalProxy


def _get_current_rwc_api_client():
    """Return current state of the search extension."""
    rwc_api_client = BaseAPIClient('reana-workflow-controller')
    return rwc_api_client._client


current_rwc_api_client = LocalProxy(_get_current_rwc_api_client)
