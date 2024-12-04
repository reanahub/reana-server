# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020, 2021, 2022, 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server administrator command line tool."""

import datetime
import logging
from pathlib import Path
import secrets
import sys
import traceback
from typing import List, Optional

import click
import requests
import tablib
from flask.cli import with_appcontext
from invenio_accounts.utils import register_user
from kubernetes.client.rest import ApiException
from reana_commons.config import (
    REANA_RESOURCE_HEALTH_COLORS,
    REANA_RUNTIME_KUBERNETES_NAMESPACE,
    REANAConfig,
)
from reana_commons.email import REANA_EMAIL_SENDER, send_email
from reana_commons.errors import REANAEmailNotificationError
from reana_commons.k8s.api_client import current_k8s_corev1_api_client
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
    Workflow,
    WorkspaceRetentionRule,
    WorkspaceRetentionRuleStatus,
)
from reana_db.utils import update_workspace_retention_rules

from reana_server.api_client import current_rwc_api_client
from reana_server.config import ADMIN_USER_ID, REANA_HOSTNAME
from reana_server.reana_admin.check_workflows import check_workspaces
from reana_server.reana_admin.options import (
    add_user_options,
    add_workflow_option,
    admin_access_token_option,
)
from reana_server.reana_admin.retention_rule_deleter import RetentionRuleDeleter
from reana_server.status import STATUS_OBJECT_TYPES
from reana_server.utils import (
    JinjaEnv,
    _create_user,
    _export_users,
    _get_user_by_criteria,
    _get_users,
    _import_users,
    _validate_email,
    _validate_password,
    create_user_workspace,
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
        response = _get_users(id, email, user_access_token)
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
        response = _create_user(email, user_access_token)
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
        csv_file = _export_users()
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
        _import_users(file_)
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
            sender_email=REANA_EMAIL_SENDER,
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
            sender_email=REANA_EMAIL_SENDER,
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
        response = _get_users(id, email, user_access_token)
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
@admin_access_token_option
@click.pass_context
def set_quota_limit(
    ctx, emails, resource_type, resource_name, limit, admin_access_token
):
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
    help="""Set default quota limits for users who do not have any custom limits
         defined.

    Note that any previously set user limits, either via old defaults or via
    custom settings, will be kept during the upgrade, and won't be automatically
    updated to match the new default limit value.""",
)
@admin_access_token_option
@click.pass_context
def set_default_quota_limit(ctx, admin_access_token: str):
    """Set default quota limits for users who do not have any custom limits defined."""
    users_without_quota_limits = (
        Session.query(User)
        .filter(
            User.id_.in_(
                Session.query(UserResource.user_id).filter(
                    UserResource.quota_limit == 0
                )
            )
        )
        .all()
    )

    if not users_without_quota_limits:
        click.secho("There are no users without quota limits.", fg="green")
        sys.exit(0)

    resources = Resource.query.all()

    for user in users_without_quota_limits:
        for resource in resources:
            user_resource = UserResource.query.filter_by(
                user_id=user.id_, resource_id=resource.id_
            ).first()

            if user_resource and user_resource.quota_limit == 0:
                # If no limit exists, set the default limit
                default_limit = DEFAULT_QUOTA_LIMITS.get(resource.type_.name)
                if default_limit is not None and default_limit != 0:
                    ctx.invoke(
                        set_quota_limit,
                        emails=[user.email],
                        resource_name=resource.name,
                        resource_type=resource.type_.name,
                        limit=default_limit,
                        admin_access_token=admin_access_token,
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
@admin_access_token_option
def queue_consume(
    queue_name: str,
    key: Optional[str],
    values_to_delete: List[str],
    interactive: bool,
    admin_access_token: str,
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


@reana_admin.command()
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show the pending retention rules without applying them. [default=False]",
)
@click.option(
    "--force-date",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]),
    help="Force desired date and time when deciding which rules to apply.",
)
@click.option(
    "--yes-i-am-sure",
    is_flag=True,
    help="Do not ask for confirmation when doing potentially dangerous operations.",
)
@add_user_options
@add_workflow_option()
@admin_access_token_option
def retention_rules_apply(
    dry_run: bool,
    force_date: Optional[datetime.datetime],
    yes_i_am_sure: bool,
    user: Optional[User],
    workflow: Optional[Workflow],
    admin_access_token: str,
) -> None:
    """Apply pending retentions rules."""
    if user and workflow and user.id_ != workflow.owner_id:
        click.secho("The specified user is not the owner of the workflow.", fg="red")
        sys.exit(1)

    current_time = datetime.datetime.now()
    if force_date:
        # Warn the admin that using `force-date` can be dangerous
        if not yes_i_am_sure and not dry_run:
            if workflow:
                subject = f"workflow {workflow.id_}"
            elif user:
                subject = f"user {user.email} and **ALL** their workflows"
            else:
                subject = "**ALL THE WORKFLOWS**"
            click.confirm(
                click.style(
                    f"Deleting non-retained workspace files for {subject} "
                    f"as if it were {force_date}.\n"
                    "Are you sure you want to continue?",
                    fg="red",
                    bold=True,
                ),
                abort=True,
            )
        current_time = force_date
        click.echo(f"The current time is forced to be {current_time}")

    candidate_rules = WorkspaceRetentionRule.query
    if workflow:
        candidate_rules = workflow.retention_rules
    elif user:
        candidate_rules = WorkspaceRetentionRule.query.join(user.workflows.subquery())

    click.echo("Setting the status of all the rules that will be applied to `pending`")
    active_rules = candidate_rules.filter(
        WorkspaceRetentionRule.status == WorkspaceRetentionRuleStatus.active,
        WorkspaceRetentionRule.apply_on < current_time,
    )
    if not dry_run:
        update_workspace_retention_rules(
            active_rules, WorkspaceRetentionRuleStatus.pending
        )

    click.echo("Fetching all the pending rules")
    pending_rules = candidate_rules.filter(
        WorkspaceRetentionRule.status == WorkspaceRetentionRuleStatus.pending
    )
    if not dry_run:
        pending_rules = pending_rules.all()
    else:
        pending_rules = pending_rules.union(active_rules).all()

    if not pending_rules:
        click.echo("No rules to be applied!")

    for rule in pending_rules:
        if not Path(rule.workflow.workspace_path).exists():
            # workspace was deleted, set rule as if it was already applied
            click.secho(
                f"Workspace {rule.workflow.workspace_path} of rule {rule.id_} does not exist, "
                "setting the status to `applied`",
                fg="red",
            )
            update_workspace_retention_rules(
                [rule], WorkspaceRetentionRuleStatus.applied
            )
            continue
        # if there are errors, the status of the rule will be reset to `active`
        # so that the rule will be applied again at the next execution of the cronjob
        next_status = WorkspaceRetentionRuleStatus.active
        try:
            RetentionRuleDeleter(rule).apply_rule(dry_run)
            next_status = WorkspaceRetentionRuleStatus.applied
        except Exception as e:
            click.secho(f"Error while applying rule {rule.id_}: {e}", fg="red")
            logging.debug(e, exc_info=True)

        if not dry_run:
            click.echo(f"Setting the status of rule {rule.id_} to `{next_status.name}`")
            update_workspace_retention_rules([rule], next_status)


@reana_admin.command()
@add_workflow_option(required=True)
@click.option(
    "--days",
    "-d",
    help="Number of days to extend the rules.",
    required=True,
    type=click.IntRange(min=0),
)
@admin_access_token_option
def retention_rules_extend(
    workflow: Optional[Workflow], days: int, admin_access_token: str
) -> None:
    """Extend active retentions rules."""
    click.echo("Fetching all the active rules")
    active_rules = WorkspaceRetentionRule.query.filter(
        WorkspaceRetentionRule.status == WorkspaceRetentionRuleStatus.active,
        WorkspaceRetentionRule.workflow_id == workflow.id_,
    ).all()

    if not active_rules:
        click.echo("There are no rules to be extended for this workflow!")

    for rule in active_rules:
        apply_on = rule.apply_on + datetime.timedelta(days=days)
        click.secho(
            f"Extending rule {rule.id_}: "
            f"previous execution time '{rule.apply_on}' is extended to '{apply_on}'",
            fg="green",
        )
        rule.retention_days += days
        rule.apply_on = apply_on
        Session.add(rule)
    Session.commit()


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
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    help="Show all workflows/sessions/workspaces, even if in-sync.",
)
@admin_access_token_option
def check_workflows(
    date_start: datetime.datetime,
    date_end: Optional[datetime.datetime],
    show_all: bool,
    admin_access_token: str,
) -> None:
    """Check consistency of selected workflow run statuses between database, message queue and Kubernetes."""
    from .check_workflows import (
        InfoCollectionError,
        check_interactive_sessions,
        check_workflows,
        display_results,
    )

    click.secho("Checking if workflows are in-sync...", fg="yellow")
    workflows_in_sync = True
    try:
        in_sync_workflows, out_of_sync_workflows, total_workflows = check_workflows(
            date_start, date_end
        )
    except InfoCollectionError as error:
        workflows_in_sync = False
        logging.exception(error)
    else:
        if not out_of_sync_workflows:
            click.secho("All workflows are in-sync!", fg="green")

        if show_all and in_sync_workflows:
            click.secho(
                f"\nIn-sync workflows ({len(in_sync_workflows)} out of {total_workflows})\n",
                fg="green",
            )
            display_results(in_sync_workflows)

        if out_of_sync_workflows:
            workflows_in_sync = False
            click.secho(
                f"\nOut-of-sync workflows ({len(out_of_sync_workflows)} out of {total_workflows})\n",
                fg="red",
            )
            display_results(out_of_sync_workflows)

    click.secho("\nChecking if sessions are in-sync...", fg="yellow")
    sessions_in_sync = True
    try:
        (
            in_sync_sessions,
            out_of_sync_sessions,
            pods_without_session,
            total_sessions,
        ) = check_interactive_sessions()
    except InfoCollectionError as error:
        sessions_in_sync = False
        logging.exception(error)
    else:
        if not out_of_sync_sessions:
            click.secho("All sessions are in-sync!", fg="green")

        if show_all and in_sync_sessions:
            click.secho(
                f"\nIn-sync sessions ({len(in_sync_sessions)} out of {total_sessions})\n",
                fg="green",
            )
            display_results(in_sync_sessions)

        if out_of_sync_sessions:
            sessions_in_sync = False
            click.secho(
                f"\nOut-of-sync sessions ({len(out_of_sync_sessions)} out of {total_sessions})\n",
                fg="red",
            )
            display_results(out_of_sync_sessions)

        if pods_without_session:
            sessions_in_sync = False
            click.secho(
                f"\nSession pods without session in the database ({len(pods_without_session)} found)\n",
                fg="red",
            )
            display_results(pods_without_session)

    click.secho("\nChecking if workspaces on shared volume are in-sync...", fg="yellow")
    extra_workspaces = check_workspaces()
    if extra_workspaces:
        click.secho(
            "\nOut-of-sync workspaces found on shared volume\n",
            fg="red",
        )
        display_results(
            extra_workspaces, headers=["workspace", "name", "user", "status"]
        )
    else:
        click.secho("All workspaces found on shared volume are in-sync!", fg="green")

    if workflows_in_sync and sessions_in_sync and not extra_workspaces:
        click.secho("\nOK", fg="green")
    else:
        click.secho("\nFAILED", fg="red")
        sys.exit(1)


@reana_admin.command()
@click.option(
    "--days",
    "-d",
    help="Close interactive sessions that are inactive for more than the specified number of days.",
    required=True,
    type=click.IntRange(min=0),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show which interactive sessions would be closed, without closing them. [default=False]",
)
@admin_access_token_option
def interactive_session_cleanup(
    days: int, dry_run: bool, admin_access_token: str
) -> None:
    """Close inactive interactive sessions."""
    click.echo(
        f"Starting to close interactive sessions running longer than {days} days.."
    )
    click.echo("Fetching interactive session pods..")
    try:
        pods = current_k8s_corev1_api_client.list_namespaced_pod(
            namespace=REANA_RUNTIME_KUBERNETES_NAMESPACE,
            label_selector="reana_workflow_mode=session",
        ).items
    except ApiException as e:
        click.secho(f"Couldn't fetch a list of pods: {e}", fg="red", err=True)
        sys.exit(1)

    if not pods:
        click.echo("There are no interactive sessions to process!")

    for pod in pods:
        try:
            pod_name = pod.metadata.name
            workflow_id = pod.metadata.labels["reana-run-session-workflow-uuid"]
            user_id = pod.metadata.labels["reana-run-session-owner-uuid"]
            container_args = pod.spec.containers[0].args
            # find `--NotebookApp.token` session container argument and parse the user token value from it
            token = next(filter(lambda a: "token" in a, container_args)).split("'")[1]
        except Exception as e:
            click.secho(
                f"Couldn't parse user details from '{pod_name}' session metadata: {e}",
                fg="red",
                err=True,
            )
            logging.debug(e, exc_info=True)
            continue

        try:
            session_status = requests.get(
                f"http://reana-run-session-{workflow_id}.{REANA_RUNTIME_KUBERNETES_NAMESPACE}:8081/{workflow_id}/api/status",
                headers={"Authorization": f"token {token}"},
            ).json()
        except Exception as e:
            click.secho(
                f"Couldn't fetch interactive session '{pod_name}' status: {e}",
                fg="red",
                err=True,
            )
            logging.debug(e, exc_info=True)
            continue

        last_activity = datetime.datetime.strptime(
            session_status["last_activity"], "%Y-%m-%dT%H:%M:%S.%f%z"
        ).replace(tzinfo=None)
        duration = datetime.datetime.utcnow() - last_activity
        if duration.days >= days:
            if dry_run:
                click.echo(
                    f"Interactive session '{pod_name}' would be closed, it was updated {duration.days} days ago."
                )
                continue
            try:
                (
                    response,
                    _,
                ) = current_rwc_api_client.api.close_interactive_session(
                    user=user_id, workflow_id_or_name=workflow_id
                ).result()
                click.secho(
                    f"Interactive session '{pod_name}' has been closed.", fg="green"
                )
            except Exception as e:
                click.secho(
                    f"Couldn't close interactive session '{pod_name}': {e}",
                    fg="red",
                    err=True,
                )
                logging.debug(e, exc_info=True)
        else:
            click.echo(
                f"Interactive session '{pod_name}' was updated {duration.days} days ago. Leaving opened."
            )
