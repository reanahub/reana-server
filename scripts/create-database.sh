#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# Copyright (C) 2020, 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

set -e

# Create REANA DB
echo 'Creating REANA database...'
reana-db init
reana-db alembic init
reana-db quota create-default-resources
echo 'REANA database created.'

# Create Invenio DB
echo 'Creating Invenio database...'
invenio db init
invenio db create
echo 'Invenio database created.'
