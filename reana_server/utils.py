# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# REANA is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# REANA; if not, write to the Free Software Foundation, Inc., 59 Temple Place,
# Suite 330, Boston, MA 02111-1307, USA.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization or
# submit itself to any jurisdiction.
"""REANA-Server utils."""

from uuid import UUID

import fs
from flask import current_app as app
from reana_commons.utils import get_user_analyses_dir
from reana_commons.models import User
from reana_commons.database import Session


def is_uuid_v4(uuid_or_name):
    """Check if given string is a valid UUIDv4."""
    # Based on https://gist.github.com/ShawnMilo/7777304
    try:
        uuid = UUID(uuid_or_name, version=4)
    except Exception:
        return False

    return uuid.hex == uuid_or_name.replace('-', '')


def create_user_space(user_id, org):
    """Create analyses directory for `user_id`."""
    reana_fs = fs.open_fs(app.config['SHARED_VOLUME_PATH'])
    user_analyses_dir = get_user_analyses_dir(org, user_id)
    if not reana_fs.exists(user_analyses_dir):
        reana_fs.makedirs(user_analyses_dir)


def validate_token(token):
    """Validate that the token provided is valid."""
    token_found = Session.query(User).filter_by(api_key=token).one_or_none()
    if not token_found:
        raise ValueError('Token not valid.')
    return True
