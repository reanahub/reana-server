# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server utils."""

import csv
import io
import secrets
from uuid import UUID

import fs
from flask import current_app as app
from reana_db.database import Session
from reana_db.models import User
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from reana_server.config import ADMIN_USER_ID


def is_uuid_v4(uuid_or_name):
    """Check if given string is a valid UUIDv4."""
    # Based on https://gist.github.com/ShawnMilo/7777304
    try:
        uuid = UUID(uuid_or_name, version=4)
    except Exception:
        return False

    return uuid.hex == uuid_or_name.replace('-', '')


def create_user_workspace(user_workspace_path):
    """Create user workspace directory."""
    reana_fs = fs.open_fs(app.config['SHARED_VOLUME_PATH'])
    if not reana_fs.exists(user_workspace_path):
        reana_fs.makedirs(user_workspace_path)


def get_user_from_token(access_token):
    """Validate that the token provided is valid."""
    user = Session.query(User).filter_by(access_token=access_token).\
        one_or_none()
    if not user:
        raise ValueError('Token not valid.')
    return str(user.id_)


def _get_users(_id, email, user_access_token, admin_access_token):
    """Return all users matching search criteria."""
    admin = Session.query(User).filter_by(id_=ADMIN_USER_ID).one_or_none()
    if admin_access_token != admin.access_token:
        raise ValueError('Admin access token invalid.')
    search_criteria = dict()
    if _id:
        search_criteria['id_'] = _id
    if email:
        search_criteria['email'] = email
    if user_access_token:
        search_criteria['access_token'] = user_access_token
    users = Session.query(User).filter_by(**search_criteria).all()
    return users


def _create_user(email, user_access_token, admin_access_token):
    """Create user with provided credentials."""
    try:
        admin = Session.query(User).filter_by(id_=ADMIN_USER_ID).one_or_none()
        if admin_access_token != admin.access_token:
            raise ValueError('Admin access token invalid.')
        if not user_access_token:
            user_access_token = secrets.token_urlsafe(16)
        user_parameters = dict(access_token=user_access_token)
        user_parameters['email'] = email
        user = User(**user_parameters)
        Session.add(user)
        Session.commit()
    except (InvalidRequestError, IntegrityError) as e:
        Session.rollback()
        raise ValueError('Could not create user, '
                         'possible constraint violation')
    return user


def _export_users(admin_access_token):
    """Export all users in database as csv.

    :param admin_access_token: Admin access token.
    :type admin_access_token: str
    """
    admin = User.query.filter_by(id_=ADMIN_USER_ID).one_or_none()
    if admin_access_token != admin.access_token:
        raise ValueError('Admin access token invalid.')
    csv_file_obj = io.StringIO()
    csv_writer = csv.writer(csv_file_obj, dialect='unix')
    for user in User.query.all():
        csv_writer.writerow([user.id_, user.email, user.access_token])
    return csv_file_obj


def _import_users(admin_access_token, users_csv_file):
    """Import list of users to database.

    :param admin_access_token: Admin access token.
    :type admin_access_token: str
    :param users_csv_file: CSV file object containing a list of users.
    :type users_csv_file: _io.TextIOWrapper
    """
    admin = User.query.filter_by(id_=ADMIN_USER_ID).one_or_none()
    if admin_access_token != admin.access_token:
        raise ValueError('Admin access token invalid.')
    csv_reader = csv.reader(users_csv_file)
    users = []
    for row in csv_reader:
        user = User(id_=row[0], email=row[1], access_token=row[2])
        Session.add(user)
    Session.commit()
    Session.remove()
