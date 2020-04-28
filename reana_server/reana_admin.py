# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server administrator command line tool."""

import functools
import logging
import os
import secrets
import sys
import traceback

import click
import tablib
from flask.cli import FlaskGroup, with_appcontext
from reana_commons.utils import click_table_printer
from reana_db.database import Session, init_db
from reana_db.models import AuditLogAction, User, UserTokenStatus

from reana_server.config import ADMIN_USER_ID
from reana_server.factory import create_app
<<<<<<< HEAD
from reana_server.status import STATUS_OBJECT_TYPES
from reana_server.utils import (_create_user, _export_users, _get_users,
=======
from reana_server.utils import (_create_user, _export_users,
                                _get_user_by_criteria, _get_users,
>>>>>>> cli: add grant & revoke access token commands
                                _import_users, create_user_workspace)


def admin_access_token_option(func):
    """Click option to load admin access token."""
    @click.option(
        '--admin-access-token',
        required=True,
        default=os.environ.get('REANA_ADMIN_ACCESS_TOKEN'),
        help='The access token of an administrator.')
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


def create_reana_flask_app():
    """Create the REANA Flask app, removing existing Invenio commands."""
    app = create_app()
    app.cli.commands.clear()
    return app


@click.group(cls=FlaskGroup, add_default_commands=False,
             create_app=create_reana_flask_app)
def reana_admin():
    """REANA administration commands."""


@reana_admin.command('db-init')
def db_init():
    """Initialise database."""
    try:
        init_db()
        click.echo(click.style('DB Created.', fg='green'))
    except Exception as e:
        click.echo('Something went wrong: {0}'.format(e))
        sys.exit(1)


@reana_admin.command('users-create-default')
@click.argument('email')
@click.option('-i', '--id', 'id_',
              default=ADMIN_USER_ID)
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


@reana_admin.command(
    'users-list',
    help='List users according to the search criteria.')
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
    '--json',
    'output_format',
    flag_value='json',
    default=None,
    help='Get output in JSON format.')
@admin_access_token_option
@click.pass_context
def list_users(ctx, id, email, user_access_token, admin_access_token,
               output_format):
    """List users according to the search criteria."""
    try:
        response = _get_users(id, email, user_access_token, admin_access_token)
        headers = ['id', 'email', 'access_token', 'access_token_status']
        data = []
        for user in response:
            data.append((str(user.id_), user.email, str(user.access_token),
                         str(user.access_token_status)))
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


@reana_admin.command(
    'users-create',
    help='Create a new user.')
@click.option(
    '-e',
    '--email',
    required=True,
    help='The email of the user.')
@click.option(
    '--user-access-token',
    help='The access token of the user.')
@admin_access_token_option
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


@reana_admin.command('users-export')
@admin_access_token_option
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


@reana_admin.command('users-import')
@admin_access_token_option
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


@reana_admin.command(
    'token-grant', help='Grant a token to the selected user.')
@admin_access_token_option
@click.option(
    '--id',
    'id_',
    help='The id of the user.')
@click.option(
    '-e',
    '--email',
    help='The email of the user.')
@click.option(
    '-f',
    '--force',
    is_flag=True,
    default=False,
    help='Grant token even if user has a revoked access token.')
def token_grant(admin_access_token, id_, email, force):
    """Grant a token to the selected user."""
    try:
        admin = User.query.filter_by(id_=ADMIN_USER_ID).one_or_none()
        if admin_access_token != admin.access_token:
            raise ValueError('Admin access token invalid.')
        user = _get_user_by_criteria(id_, email)
        if user.access_token:
            click.secho(f'User {user.id_} ({user.email}) has already an active'
                        ' access token')
            sys.exit(1)
        if not force and \
           user.access_token_status in [UserTokenStatus.revoked.name, None]:
            click.secho(f'User {user.id_} ({user.email}) access token status'
                        ' is {user.access_token_status}, if you want to'
                        ' proceed in any case use --force option')
            sys.exit(1)
        user_granted_token = secrets.token_urlsafe(16)
        user.access_token = user_granted_token
        Session.commit()
        msg = (f'Token for user {user.id_} ({user.email}) granted.\n'
               f'\nToken: {user_granted_token}')
        admin.log_action(AuditLogAction.grant_token, {'reana_admin': msg})
        # send notification to user by email

        click.secho(msg, fg='green')
    except Exception as e:
        click.secho(
            'Something went wrong while granting token:\n{}'.format(e),
            fg='red', err=True)


@reana_admin.command(
    'token-revoke', help='Revoke selected user\'s token.')
@admin_access_token_option
@click.option(
    '--id',
    'id_',
    help='The id of the user.')
@click.option(
    '-e',
    '--email',
    help='The email of the user.')
def token_revoke(admin_access_token, id_, email):
    """Revoke selected user's token."""
    admin = User.query.filter_by(id_=ADMIN_USER_ID).one_or_none()
    if admin_access_token != admin.access_token:
        raise ValueError('Admin access token invalid.')
    user = _get_user_by_criteria(id_, email)
    if not user.access_token:
        click.secho(f'User {user.id_} ({user.email}) does not have an active'
                    ' access token')
        sys.exit(1)
    user.active_token.status = UserTokenStatus.revoked
    Session.commit()
    msg = f'User\'s {user.id_} ({user.email}) token successfully revoked'
    admin.log_action(AuditLogAction.revoke_token, {'reana_admin': msg})
    # send notification to user by email
    click.secho(msg, fg='green')
    click.secho(f'User\'s {user_id} ({user_email}) token successfully revoked',
                fg='green')


@reana_admin.command(help='Get a status report of the REANA system.')
@click.option(
    '--type',
    'types',
    multiple=True,
    default=('all',),
    type=click.Choice(list(STATUS_OBJECT_TYPES.keys()) + ['all'],
                      case_sensitive=False),
    help='Type of information to be displayed?')
@admin_access_token_option
def status_report(types, admin_access_token):
    """Retrieve a status report summary of the REANA system."""
    def _format_statuses(type_, statuses):
        """Format statuses dictionary object."""
        click.echo(type_.upper())
        for stat_name, stat_value in statuses.items():
            click.echo(f'{stat_name}: {stat_value}')

    types = STATUS_OBJECT_TYPES.keys() if 'all' in types else types
    for type_ in types:
        statuses_obj = STATUS_OBJECT_TYPES[type_]()
        statuses = statuses_obj.get_status()
        _format_statuses(type_, statuses)
        click.echo('\n')
