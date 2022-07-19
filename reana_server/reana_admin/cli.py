# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020, 2021, 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server administrator command line tool."""

import datetime
import logging
import secrets
import sys
import traceback
from typing import List, Optional

import click
import tablib
from flask.cli import with_appcontext
from invenio_accounts.utils import register_user
from reana_commons.config import REANAConfig, REANA_RESOURCE_HEALTH_COLORS
from reana_commons.email import send_email
from reana_commons.errors import REANAEmailNotificationError
from reana_commons.utils import click_table_printer
from reana_db.config import DEFAULT_QUOTA_LIMITS
from reana_db.database import Session
from reana_db.models import (
    AuditLogAction,
    QuotaHealth,
    Resource,
    ResourceType,
    User,
    UserResource,
    UserTokenStatus,
    WorkspaceRetentionRule,
    WorkspaceRetentionRuleStatus,
)
from reana_db.utils import update_workspace_retention_rules

from reana_server.config import ADMIN_EMAIL, ADMIN_USER_ID, REANA_HOSTNAME
from reana_server.decorators import admin_access_token_option
from reana_server.reana_admin.retention_rule_deleter import RetentionRuleDeleter
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


@click.group()
def reana_admin():
    """REANA administration commands."""


@reana_admin.command("create-admin-user")
@click.option(
    "-e",
    "--email",
    callback=_validate_email,
    required=True,
    help="The email of the admin user.",
)
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
@click.option(
    "-e",
    "--email",
    callback=_validate_email,
    required=True,
    help="The email of the user.",
)
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

    def _print_row(data, column_widths):
        return (
            "  {email:<{email_width}} | {used:>{used_width}} | {limit:>{limit_width}} "
            "| {percentage:>{percentage_width}}\n".format(**data, **column_widths)
        )

    def _format_quota_statuses(type_, statuses):
        formatted_statuses = type_.upper()
        for status_name, data in statuses.items():
            if not data:
                continue
            formatted_statuses += f"\n{status_name}:\n"
            columns = {
                "email": "EMAIL",
                "used": "USED",
                "limit": "LIMIT",
                "percentage": "PERCENTAGE",
            }
            column_widths = {
                "email_width": max([len(item["email"]) for item in data]),
                "used_width": max([len(item["used"]) for item in data]),
                "limit_width": max([len(item["limit"]) for item in data]),
                "percentage_width": len("percentage"),
            }
            formatted_statuses += _print_row(columns, column_widths)
            for row in data:
                formatted_statuses += _print_row(row, column_widths)

        return formatted_statuses

    def _format_statuses(type_, statuses):
        """Format statuses dictionary object."""
        if type_ == "quota-usage":
            return _format_quota_statuses(type_, statuses)
        formatted_statuses = type_.upper() + "\n"
        for stat_name, stat_value in statuses.items():
            formatted_statuses += f"{stat_name}: {stat_value}\n"

        return formatted_statuses

    try:
        types = STATUS_OBJECT_TYPES.keys() if "all" in types else types
        status_report_output = ""
        hostname = REANA_HOSTNAME or "REANA service"
        for type_ in types:
            statuses_obj = STATUS_OBJECT_TYPES[type_]()
            statuses = statuses_obj.get_status()
            status_report_output += _format_statuses(type_, statuses) + "\n"

        status_report_body = (
            f"Status report for {hostname}\n---\n{status_report_output}"
        )

        if email:
            send_email(email, f"{hostname} system status report", status_report_body)
            click.echo(f"Status report successfully sent by email to {email}.")
        else:
            click.echo(status_report_body)
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


@reana_admin.command("quota-usage", help="List quota usage of users.")
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
@click.option(
    "-h",
    "--human-readable",
    "human_readable",
    is_flag=True,
    default=False,
    callback=lambda ctx, param, value: "human_readable" if value else "raw",
    help="Show quota usage values in human readable format.",
)
@admin_access_token_option
@click.pass_context
def list_quota_usage(
    ctx, id, email, user_access_token, admin_access_token, output_format, human_readable
):
    """List quota usage of users."""
    try:
        response = _get_users(id, email, user_access_token, admin_access_token)
        headers = ["id", "email", "cpu-used", "cpu-limit", "disk-used", "disk-limit"]
        health_order = {
            QuotaHealth.healthy.name: 0,
            QuotaHealth.warning.name: 1,
            QuotaHealth.critical.name: 2,
        }
        data = []
        colours = []
        health = []
        for user in response:
            quota_usage = user.get_quota_usage()
            disk, cpu = quota_usage.get("disk"), quota_usage.get("cpu")
            data.append(
                (
                    str(user.id_),
                    user.email,
                    cpu.get("usage").get(human_readable),
                    cpu.get("limit", {}).get(human_readable) or "-",
                    disk.get("usage").get(human_readable),
                    disk.get("limit", {}).get(human_readable) or "-",
                )
            )
            health_ordered = max(
                [
                    disk.get("health", QuotaHealth.healthy.name),
                    cpu.get("health", QuotaHealth.healthy.name),
                ],
                key=lambda key: health_order[key],
            )
            colours.append(REANA_RESOURCE_HEALTH_COLORS[health_ordered])
            health.append(health_ordered)

        if data and colours and health:
            data, colours, _ = (
                list(t)
                for t in zip(
                    *sorted(
                        zip(data, colours, health),
                        key=lambda t: health_order[t[2]],
                        reverse=True,
                    )
                )
            )

        if output_format:
            tablib_data = tablib.Dataset()
            tablib_data.headers = headers
            for row in data:
                tablib_data.append(row)

            click.echo(tablib_data.export(output_format))
        else:
            click_table_printer(headers, [], data, colours)

    except Exception as e:
        logging.debug(traceback.format_exc())
        logging.debug(str(e))
        click.echo(
            click.style("User could not be retrieved: \n{}".format(str(e)), fg="red"),
            err=True,
        )


@reana_admin.command("quota-resources", help="List available quota resources.")
@click.pass_context
def list_quota_resources(ctx):
    """List quota resources."""
    click.echo("Available resources are:")
    for resource in Resource.query:
        click.echo(f"{resource.type_.name} ({resource.name})")


@reana_admin.command(
    "quota-set", help="Set quota limits to the given users per resource."
)
@click.option(
    "-e",
    "--email",
    "emails",
    multiple=True,
    required=True,
    help=(
        "The emails of the users. "
        "E.g. --email johndoe@example.org --email janedoe@example.org"
    ),
)
@click.option(
    "--resource", "-r", "resource_type", help="Specify quota resource. e.g. cpu, disk."
)
@click.option("--resource-name", "-n", help="Name of resource.")
@click.option(
    "--limit", "-l", help="New limit in canonical unit.", required=True, type=int
)
@click.pass_context
def set_quota_limit(ctx, emails, resource_type, resource_name, limit):
    """Set quota limits to the given users per resource."""
    try:
        for email in emails:
            error_msg = None
            resource = None
            user = _get_user_by_criteria(None, email)

            if resource_name:
                resource = Resource.query.filter_by(name=resource_name).one_or_none()
            elif resource_type in ResourceType._member_names_:
                resources = Resource.query.filter_by(type_=resource_type).all()
                if resources and len(resources) > 1:
                    click.secho(
                        f"ERROR: There are more than one `{resource_type}` resource. "
                        "Please provide resource name with `--resource-name` option to specify the exact resource.",
                        fg="red",
                        err=True,
                    )
                    sys.exit(1)
                else:
                    resource = resources[0]

            if not user:
                error_msg = f"ERROR: Provided user {email} does not exist."
            elif not resource:
                resources = [
                    f"{resource.type_.name} ({resource.name})"
                    for resource in Resource.query
                ]
                error_msg = (
                    f"ERROR: Provided resource `{resource_name or resource_type}` does not exist. "
                    if resource_name or resource_type
                    else "ERROR: Please provide a resource. "
                )
                error_msg += f"Available resources are: {', '.join(resources)}."
            if error_msg:
                click.secho(
                    error_msg,
                    fg="red",
                    err=True,
                )
                sys.exit(1)

            user_resource = UserResource.query.filter_by(
                user=user, resource=resource
            ).one_or_none()
            if user_resource:
                user_resource.quota_limit = limit
                Session.add(user_resource)
            else:
                # Create user resource in case there isn't one. Useful for old users.
                user.resources.append(
                    UserResource(
                        user_id=user.id_,
                        resource_id=resource.id_,
                        quota_limit=limit,
                        quota_used=0,
                    )
                )
        Session.commit()
        click.secho(
            f"Quota limit {limit} for '{resource.type_.name} ({resource.name})' successfully set to users {emails}.",
            fg="green",
        )
    except Exception as e:
        logging.debug(traceback.format_exc())
        logging.debug(str(e))
        click.echo(
            click.style("Quota could not be set: \n{}".format(str(e)), fg="red"),
            err=True,
        )


@reana_admin.command(
    "quota-set-default-limits",
    help="Set default quota limits to users that do not have any.",
)
@click.pass_context
def set_default_quota_limit(ctx):
    """Set default quota limits to users that do not have any."""
    users_without_quota_limits = User.query.filter(~User.resources.any()).all()
    if not users_without_quota_limits:
        click.secho("There are no users without quota limits.", fg="green")
        sys.exit(0)
    for resource in Resource.query:
        ctx.invoke(
            set_quota_limit,
            emails=[user.email for user in users_without_quota_limits],
            resource_name=resource.name,
            resource_type=resource.type_.name,
            limit=DEFAULT_QUOTA_LIMITS.get(resource.type_.name),
        )


@reana_admin.command("queue-consume")
@click.option(
    "--queue-name",
    "-q",
    required=True,
    type=str,
    help="Name of the queue that will be consumed, e.g workflow-submission",
)
@click.option(
    "--key",
    "-k",
    type=str,
    help="Key of the property that will be used to filter the messages in the queue, e.g workflow_name_or_id",
)
@click.option(
    "--values-to-delete",
    "-v",
    multiple=True,
    help="List of property values used to filter messages that will be removed from the queue, e.g UUID of a workflow",
)
@click.option(
    "-i",
    "--interactive",
    is_flag=True,
    default=False,
    help="Manually decide which messages to remove from the queue.",
)
def queue_consume(
    queue_name: str,
    key: Optional[str],
    values_to_delete: List[str],
    interactive: bool,
):
    """Start consuming specified queue and remove selected messages.

    By default, you will need to specify either "-k" or "-i" options otherwise the command will return an error.

    If -k option is specified, messages that have property values specified in -v will be deleted.

    If -i option is specified, for every message, user will be asked what to do.

    If -k and -i are specified together, for every message that matches property values in -v, user will be asked whether to delete it or not.
    """
    from reana_server.reana_admin.consumer import MessageConsumer

    if key is None and not interactive:
        click.secho(
            "Please provide -k (with -v) or -i options. These options can be used together or separately.",
            fg="red",
        )
        sys.exit(1)

    if key and not values_to_delete:
        click.secho(
            f"Please provide a list of property values (using the '-v' option) to filter the messages that need to be removed from the {queue_name} queue.",
            fg="red",
        )
        sys.exit(1)

    try:
        consumer = MessageConsumer(
            queue_name=queue_name,
            key=key,
            values_to_delete=list(values_to_delete),
            is_interactive=interactive,
        )
    except Exception as error:
        click.secho(
            "Error is raised during MessageConsumer initialization. Please, check if arguments are correct.",
            fg="red",
        )
        logging.exception(error)
    else:
        consumer.run()


@reana_admin.command("check-workflows")
@click.option(
    "--date-start",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]),
    default=datetime.datetime.now() - datetime.timedelta(hours=24),
    help="Default value is 24 hours ago.",
)
@click.option(
    "--date-end",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]),
    default=None,
    help="Default value is now.",
)
def check_workflows(
    date_start: Optional[datetime.datetime], date_end: Optional[datetime.datetime]
) -> None:
    """Check consistency of selected workflow run statuses between database, message queue and Kubernetes."""
    from .check_workflows import check_workflows

    check_workflows(date_start, date_end)


@reana_admin.command()
def retention_rules_apply() -> None:
    """Apply pending retentions rules."""
    current_time = datetime.datetime.now()
    pending_rules = WorkspaceRetentionRule.query.filter(
        WorkspaceRetentionRule.status == WorkspaceRetentionRuleStatus.active,
        WorkspaceRetentionRule.apply_on < current_time,
    ).all()
    if not pending_rules:
        click.echo("No rules to be applied!")
    for rule in pending_rules:
        RetentionRuleDeleter(rule).apply_rule()
        update_workspace_retention_rules([rule], WorkspaceRetentionRuleStatus.applied)
