# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
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

"""REANA Workflow Controller command line interface."""
import logging
import os
import secrets
import sys
import traceback

import click
import tablib
from flask.cli import with_appcontext
from reana_commons.utils import click_table_printer
from reana_db.database import Session, init_db
from reana_db.models import User

from reana_server import config
from reana_server.utils import _create_user, _get_users, create_user_workspace


@click.group()
def db():
    """Database management commands."""


@db.command('init')
def db_init():
    """Initialise database."""
    try:
        init_db()
        click.echo(click.style('DB Created.', fg='green'))
    except Exception as e:
        click.echo('Something went wrong: {0}'.format(e))
        sys.exit(1)


@click.group(
    help='All interaction related to user management on REANA cloud.')
@click.pass_context
def users(ctx):
    """Top level wrapper for user related interaction."""
    logging.debug(ctx.info_name)


@users.command('create_default')
@click.argument('email')
@click.option('-i', '--id', 'id_',
              default=config.ADMIN_USER_ID)
@with_appcontext
def users_create_default(email, id_):
    """Create new user."""
    user_characteristics = {"id_": id_,
                            "email": email,
                            }
    try:
        user = User.query.filter_by(**user_characteristics).first()
        if not user:
            user_characteristics['access_token'] = secrets.token_urlsafe()
            user = User(**user_characteristics)
            create_user_workspace(user.get_user_workspace())
            Session.add(user)
            Session.commit()
            click.echo('Created 1st user with access_token: {}'.
                       format(user_characteristics['access_token']))
    except Exception as e:
        click.echo('Something went wrong: {0}'.format(e))
        sys.exit(1)


@users.command(
    'get',
    help='Get information of users matching search criteria.')
@click.option(
    '--id',
    help='The id of the user.')
@click.option(
    '-e',
    '--email',
    help='The email of the user.')
@click.option(
    '--user-access-token',
    help='The access token of the user.')
@click.option(
    '--admin-access-token',
    default=os.environ.get('REANA_ACCESS_TOKEN', None),
    help='The access token of an administrator.')
@click.option(
    '--json',
    'output_format',
    flag_value='json',
    default=None,
    help='Get output in JSON format.')
@click.pass_context
def get_users(ctx, id, email, user_access_token, admin_access_token,
              output_format):
    """Return user information."""
    try:
        response = _get_users(id, email, user_access_token, admin_access_token)
        headers = ['id', 'email', 'access_token']
        data = []
        for user in response:
            data.append((str(user.id_), user.email, user.access_token))
        if output_format:
            tablib_data = tablib.Dataset()
            tablib_data.headers = headers
            for row in data:
                tablib_data.append(row)

            click.echo(tablib_data.export(output_format))
        else:
            click_table_printer(headers, [], data)

    except Exception as e:
        logging.debug(traceback.format_exc())
        logging.debug(str(e))
        click.echo(
            click.style('User could not be retrieved: \n{}'
                        .format(str(e)), fg='red'),
            err=True)


@users.command(
    'create',
    help='Create a new user.')
@click.option(
    '-e',
    '--email',
    help='The email of the user.')
@click.option(
    '--user-access-token',
    help='The access token of the user.')
@click.option(
    '--admin-access-token',
    default=os.environ.get('REANA_ACCESS_TOKEN', None),
    help='The access token of an administrator.')
@click.pass_context
def create_user(ctx, email, user_access_token, admin_access_token):
    """Create a new user."""
    try:
        response = _create_user(email, user_access_token, admin_access_token)
        headers = ['id', 'email', 'access_token']
        data = [(str(response.id_), response.email, response.access_token)]
        click.echo(
            click.style('User was successfully created.', fg='green'))
        click_table_printer(headers, [], data)

    except Exception as e:
        logging.debug(traceback.format_exc())
        logging.debug(str(e))
        click.echo(
            click.style('User could not be created: \n{}'
                        .format(str(e)), fg='red'),
            err=True)
