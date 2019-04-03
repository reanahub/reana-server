# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""
The REANA Server offers a CLI for user account operations.

Specifically, from the CLI an administrator
can create new user accounts, and retrieve users based on given filters.

By default the server docker image is set to create the administrator account
on startup, which has the email ``info@reana.io`` and a token which is
generated using the ``secrets`` python library.

To retrieve the administrator token you can run:

.. code-block:: bash

    $ reana-cluster env --all

or enter the database pod:

.. code-block:: bash

    $ kubectl exec -ti <db-pod-name> /bin/bash

access the Postgresql database:

.. code-block:: bash

    $ psql -U reana

and get all users:

.. code-block:: sql

    > SELECT * FROM user_;

With the administrator access token, new user creation is allowed with:

.. code-block :: bash

    $ flask  users create --e=<email> --admin-access-token=<token>

Similarly, to retrieve information for all users:

.. code-block :: bash

    $ flask users get --admin-access-token=<token>
"""
import logging
import os
import secrets
import sys
import traceback

import click
import tablib
from flask.cli import with_appcontext
from reana_commons.config import REANA_LOG_FORMAT, REANA_LOG_LEVEL
from reana_commons.utils import click_table_printer
from reana_db.database import Session, init_db
from reana_db.models import User

from reana_server import config
from reana_server.scheduler import WorkflowExecutionScheduler
from reana_server.utils import (_create_user, _export_users, _get_users,
                                _import_users, create_user_workspace)


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
    """Create default user.

    This user has the administrator role
    and can retrieve other user information as well as create
    new users.
    """
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
    """Return user information. Requires the token of an administrator."""
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
    """Create a new user. Requires the token of an administrator."""
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


@users.command('export')
@click.option(
    '--admin-access-token',
    default=os.environ.get('REANA_ACCESS_TOKEN', None),
    help='The access token of an administrator.')
@click.pass_context
def export_users(ctx, admin_access_token):
    """Export all users in current REANA cluster."""
    try:
        csv_file = _export_users(admin_access_token)
        click.echo(csv_file.getvalue(), nl=False)
    except Exception as e:
        click.secho(
            'Something went wrong while importing users:\n{}'.format(e),
            fg='red', err=True)


@users.command('import')
@click.option(
    '--admin-access-token',
    default=os.environ.get('REANA_ACCESS_TOKEN', None),
    help='The access token of an administrator.')
@click.option(
    '-f',
    '--file',
    'file_',
    help='A CSV file containing a list of REANA users.',
    type=click.File())
@click.pass_context
def import_users(ctx, admin_access_token, file_):
    """Import users from file."""
    try:
        _import_users(admin_access_token, file_)
        click.secho('Users successfully imported.', fg='green')
    except Exception as e:
        click.secho(
            'Something went wrong while importing users:\n{}'.format(e),
            fg='red', err=True)


@click.command('start-scheduler')
def start_scheduler():
    """Start a workflow execution scheduler process."""
    logging.basicConfig(
        level=REANA_LOG_LEVEL,
        format=REANA_LOG_FORMAT
    )
    scheduler = WorkflowExecutionScheduler()
    scheduler.run()
