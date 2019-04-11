# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test command line application."""

import csv
import io
import secrets
import uuid

import pytest
from click.testing import CliRunner
from reana_db.models import User

from reana_server.cli import users as users_cmd


def test_export_users(default_user):
    """Test exporting all users as csv."""
    runner = CliRunner()
    expected_csv_file = io.StringIO()
    csv_writer = csv.writer(expected_csv_file, dialect='unix')
    csv_writer.writerow(
        [default_user.id_, default_user.email, default_user.access_token])
    result = runner.invoke(
        users_cmd,
        ['export', '--admin-access-token', default_user.access_token])
    assert result.output == expected_csv_file.getvalue()


def test_import_users(app, session, default_user):
    """Test importing users from CSV file."""
    runner = CliRunner()
    expected_output = 'Users successfully imported.'
    users_csv_file_name = 'reana-users.csv'
    user_id = uuid.uuid4()
    user_email = 'test@reana.io'
    user_access_token = secrets.token_urlsafe(16)
    with runner.isolated_filesystem():
        with open(users_csv_file_name, 'w') as f:
            csv_writer = csv.writer(f, dialect='unix')
            csv_writer.writerow([user_id, user_email,
                                 user_access_token])

        result = runner.invoke(
            users_cmd,
            ['import',
             '--admin-access-token', default_user.access_token,
             '--file', users_csv_file_name])
        assert expected_output in result.output
        user = session.query(User).filter_by(id_=user_id).first()
        assert user
        assert user.email == user_email
        assert user.access_token == user.access_token
