# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server administrator command line tool."""

import datetime
import functools
import logging
import os
import secrets
import sys
import traceback

import click
import tablib
from flask.cli import with_appcontext
from invenio_accounts.utils import register_user
from reana_commons.config import REANAConfig
from reana_commons.email import send_email
from reana_commons.errors import REANAEmailNotificationError
from reana_commons.utils import click_table_printer
from reana_db.database import Session
from reana_db.models import AuditLogAction, User, UserTokenStatus

from reana_server.config import ADMIN_EMAIL, ADMIN_USER_ID, REANA_HOSTNAME
from reana_server.status import STATUS_OBJECT_TYPES

from reana_server.utils import (
    _create_user,
    _export_users,
    _get_user_by_criteria,
    _get_users,
    _import_users,
    _validate_email,
    _validate_password,
    create_user_workspace,
    JinjaEnv,
)


def admin_access_token_option(func):
    """Click option to load admin access token."""

    @click.option(
        "--admin-access-token",
        required=True,
        default=os.environ.get("REANA_ADMIN_ACCESS_TOKEN"),
        help="The access token of an administrator.",
    )
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


@click.group()
def reana_admin():
    """REANA administration commands."""


@reana_admin.command("create-admin-user")
@click.option("--email", "-e", callback=_validate_email, required=True)
@click.option("--password", "-p", callback=_validate_password, required=True)
@click.option("-i", "--id", "id_", default=ADMIN_USER_ID)
@with_appcontext
def users_create_default(email, password, id_):
    """Create default user.

    This user has the administrator role
    and can retrieve other user information as well as create
    new users.
    """
    reana_user_characteristics = {
        "id_": id_,
        "email": email,
    }
    try:
        user = User.query.filter_by(**reana_user_characteristics).first()
        if not user:
            reana_user_characteristics["access_token"] = secrets.token_urlsafe(16)
            user = User(**reana_user_characteristics)
            create_user_workspace(user.get_user_workspace())
            Session.add(user)
            Session.commit()
            # create invenio user, passing `confirmed_at` to mark it as confirmed
            register_user(
                email=email, password=password, confirmed_at=datetime.datetime.now()
            )
            click.echo(reana_user_characteristics["access_token"])
    except Exception as e:
        click.echo("Something went wrong: {0}".format(e))
        sys.exit(1)


@reana_admin.command("user-list", help="List users according to the search criteria.")
@click.option("--id", help="The id of the user.")
@click.option("-e", "--email", help="The email of the user.")
@click.option("--user-access-token", help="The access token of the user.")
@click.option(
    "--json",
    "output_format",
    flag_value="json",
    default=None,
    help="Get output in JSON format.",
)
@admin_access_token_option
@click.pass_context
def list_users(ctx, id, email, user_access_token, admin_access_token, output_format):
    """List users according to the search criteria."""
    try:
        response = _get_users(id, email, user_access_token, admin_access_token)
        headers = ["id", "email", "access_token", "access_token_status"]
        data = []
        for user in response:
            data.append(
                (
                    str(user.id_),
                    user.email,
                    str(user.access_token),
                    str(user.access_token_status),
                )
            )
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
            click.style("User could not be retrieved: \n{}".format(str(e)), fg="red"),
            err=True,
        )


@reana_admin.command("user-create", help="Create a new user.")
@click.option("-e", "--email", required=True, help="The email of the user.")
@click.option("--user-access-token", help="The access token of the user.")
@admin_access_token_option
@click.pass_context
def create_user(ctx, email, user_access_token, admin_access_token):
    """Create a new user. Requires the token of an administrator."""
    try:
        response = _create_user(email, user_access_token, admin_access_token)
        headers = ["id", "email", "access_token"]
        data = [(str(response.id_), response.email, response.access_token)]
        click.echo(click.style("User was successfully created.", fg="green"))
        click_table_printer(headers, [], data)

    except Exception as e:
        logging.debug(traceback.format_exc())
        logging.debug(str(e))
        click.echo(
            click.style("User could not be created: \n{}".format(str(e)), fg="red"),
            err=True,
        )


@reana_admin.command("user-export")
@admin_access_token_option
@click.pass_context
def export_users(ctx, admin_access_token):
    """Export all users in current REANA cluster."""
    try:
        csv_file = _export_users(admin_access_token)
        click.echo(csv_file.getvalue(), nl=False)
    except Exception as e:
        click.secho(
            "Something went wrong while importing users:\n{}".format(e),
            fg="red",
            err=True,
        )


@reana_admin.command("user-import")
@admin_access_token_option
@click.option(
    "-f",
    "--file",
    "file_",
    help="A CSV file containing a list of REANA users.",
    type=click.File(),
)
@click.pass_context
def import_users(ctx, admin_access_token, file_):
    """Import users from file."""
    try:
        _import_users(admin_access_token, file_)
        click.secho("Users successfully imported.", fg="green")
    except Exception as e:
        click.secho(
            "Something went wrong while importing users:\n{}".format(e),
            fg="red",
            err=True,
        )


@reana_admin.command("token-grant", help="Grant a token to the selected user.")
@admin_access_token_option
@click.option("--id", "id_", help="The id of the user.")
@click.option("-e", "--email", help="The email of the user.")
def token_grant(admin_access_token, id_, email):
    """Grant a token to the selected user."""
    try:
        admin = User.query.filter_by(id_=ADMIN_USER_ID).one_or_none()
        if admin_access_token != admin.access_token:
            raise ValueError("Admin access token invalid.")
        user = _get_user_by_criteria(id_, email)
        error_msg = None
        if not user:
            error_msg = f"User {id_ or email} does not exist."
        elif user.access_token:
            error_msg = (
                f"User {user.id_} ({user.email}) has already an active access token."
            )
        if error_msg:
            click.secho(f"ERROR: {error_msg}", fg="red")
            sys.exit(1)
        if user.access_token_status in [UserTokenStatus.revoked.name, None]:
            click.confirm(
                f"User {user.id_} ({user.email}) access token status"
                f" is {user.access_token_status}, do you want to"
                " proceed?",
                abort=True,
            )

        user_granted_token = secrets.token_urlsafe(16)
        user.access_token = user_granted_token
        Session.commit()
        log_msg = (
            f"Token for user {user.id_} ({user.email}) granted.\n"
            f"\nToken: {user_granted_token}"
        )
        click.secho(log_msg, fg="green")
        admin.log_action(AuditLogAction.grant_token, {"reana_admin": log_msg})
        # send notification to user by email
        email_subject = "REANA access token granted"
        email_body = JinjaEnv.render_template(
            "emails/token_granted.txt",
            user_full_name=user.full_name,
            reana_hostname=REANA_HOSTNAME,
            ui_config=REANAConfig.load("ui"),
            sender_email=ADMIN_EMAIL,
        )
        send_email(user.email, email_subject, email_body)

    except click.exceptions.Abort:
        click.echo("Grant token aborted.")
    except REANAEmailNotificationError as e:
        click.secho(
            "Something went wrong while sending email:\n{}".format(e),
            fg="red",
            err=True,
        )
    except Exception as e:
        click.secho(
            "Something went wrong while granting token:\n{}".format(e),
            fg="red",
            err=True,
        )


@reana_admin.command("token-revoke", help="Revoke selected user's token.")
@admin_access_token_option
@click.option("--id", "id_", help="The id of the user.")
@click.option("-e", "--email", help="The email of the user.")
def token_revoke(admin_access_token, id_, email):
    """Revoke selected user's token."""
    try:
        admin = User.query.filter_by(id_=ADMIN_USER_ID).one_or_none()
        if admin_access_token != admin.access_token:
            raise ValueError("Admin access token invalid.")
        user = _get_user_by_criteria(id_, email)

        error_msg = None
        if not user:
            error_msg = f"User {id_ or email} does not exist."
        elif not user.access_token:
            error_msg = (
                f"User {user.id_} ({user.email}) does not have an"
                " active access token."
            )
        if error_msg:
            click.secho(f"ERROR: {error_msg}", fg="red")
            sys.exit(1)

        revoked_token = user.access_token
        user.active_token.status = UserTokenStatus.revoked
        Session.commit()
        log_msg = (
            f"User token {revoked_token} ({user.email}) was" " successfully revoked."
        )
        click.secho(log_msg, fg="green")
        admin.log_action(AuditLogAction.revoke_token, {"reana_admin": log_msg})
        # send notification to user by email
        email_subject = "REANA access token revoked"
        email_body = JinjaEnv.render_template(
            "emails/token_revoked.txt",
            user_full_name=user.full_name,
            reana_hostname=REANA_HOSTNAME,
            ui_config=REANAConfig.load("ui"),
            sender_email=ADMIN_EMAIL,
        )
        send_email(user.email, email_subject, email_body)

    except REANAEmailNotificationError as e:
        click.secho(
            "Something went wrong while sending email:\n{}".format(e),
            fg="red",
            err=True,
        )
    except Exception as e:
        click.secho(
            "Something went wrong while revoking token:\n{}".format(e),
            fg="red",
            err=True,
        )


@reana_admin.command(help="Get a status report of the REANA system.")
@click.option(
    "--type",
    "types",
    multiple=True,
    default=("all",),
    type=click.Choice(list(STATUS_OBJECT_TYPES.keys()) + ["all"], case_sensitive=False),
    help="Type of information to be displayed?",
)
@click.option(
    "-e",
    "--email",
    default=None,
    help="Send the status by email to the configured receiver.",
)
@admin_access_token_option
def status_report(types, email, admin_access_token):
    """Retrieve a status report summary of the REANA system."""

    def _format_statuses(type_, statuses):
        """Format statuses dictionary object."""
        formatted_statuses = type_.upper() + "\n"
        for stat_name, stat_value in statuses.items():
            formatted_statuses += f"{stat_name}: {stat_value}\n"

        return formatted_statuses

    try:
        types = STATUS_OBJECT_TYPES.keys() if "all" in types else types
        status_report_output = ""
        for type_ in types:
            statuses_obj = STATUS_OBJECT_TYPES[type_]()
            statuses = statuses_obj.get_status()
            status_report_output += _format_statuses(type_, statuses) + "\n"

        if email:
            status_report_body = (
                f'Status report for {REANA_HOSTNAME or "REANA service"}\n'
                "---\n" + status_report_output
            )

            send_email(email, "REANA system status report", status_report_body)
            click.echo(f"Status report successfully sent by email to {email}.")
    except REANAEmailNotificationError as e:
        click.secho(
            "Something went wrong while sending email:\n{}".format(e),
            fg="red",
            err=True,
        )
    except Exception as e:
        click.secho(
            "Something went wrong while generating the status report:\n{}".format(e),
            fg="red",
            err=True,
        )
