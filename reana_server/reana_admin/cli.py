# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2020, 2021, 2022, 2023, 2024, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server administrator command line tool."""

import datetime
import logging
from pathlib import Path
import sys
import traceback
from typing import List, Optional

import click
import requests
import tablib
from click.core import ParameterSource
from flask.cli import with_appcontext
from kubernetes.client.rest import ApiException
from reana_commons.config import (
    REANA_RESOURCE_HEALTH_COLORS,
    REANA_RUNTIME_KUBERNETES_NAMESPACE,
)
from reana_commons.email import send_email
from reana_commons.errors import REANAEmailNotificationError
from reana_commons.k8s.api_client import current_k8s_corev1_api_client
from reana_commons.utils import click_table_printer
from reana_db.config import DEFAULT_QUOTA_LIMITS
from reana_db.database import Session
from reana_db.models import (
    RunStatus,
    QuotaHealth,
    Resource,
    User,
    UserResource,
    Workflow,
    WorkspaceRetentionRule,
    WorkspaceRetentionRuleStatus,
)

from reana_server.api_client import current_rwc_api_client
from reana_server.auth.provision import link_user_identity
from reana_server.auth.sessions import delete_sessions_for_subject
from reana_server.config import ADMIN_USER_ID, REANA_HOSTNAME
from reana_server.reana_admin.check_workflows import check_workspaces
from reana_server.reana_admin.options import (
    add_user_options,
    add_workflow_option,
)
from reana_server.reana_admin.retention_rule_deleter import RetentionRuleDeleter
from reana_server.status import STATUS_OBJECT_TYPES
from reana_server.workspace_mutations import (
    WorkspaceMutationConflict,
    WorkspaceMutationUnavailable,
    workspace_mutation_lock,
)
from reana_server.utils import (
    _get_admin_user_or_raise,
    _create_user,
    _export_users,
    _get_user_by_criteria,
    _get_users,
    _UNSET,
    _import_users,
    _set_quota_period,
    _validate_email,
    create_user_workspace,
    _set_quota_limit,
)


@click.group()
def reana_admin():
    """REANA administration commands."""


def _unset_if_option_omitted(ctx, param, value):
    if ctx.get_parameter_source(param.name) == ParameterSource.DEFAULT:
        return _UNSET
    return value


@reana_admin.command("create-admin-user")
@click.option(
    "-e",
    "--email",
    callback=_validate_email,
    required=True,
    help="The email of the admin user.",
)
@click.option("-i", "--id", "id_", default=ADMIN_USER_ID)
@click.option(
    "--idp-issuer",
    help="OIDC issuer URL to link explicitly to the administrator.",
)
@click.option(
    "--idp-subject",
    help="OIDC subject to link explicitly to the administrator.",
)
@with_appcontext
def users_create_default(email, id_, idp_issuer, idp_subject):
    """Create the default administrator user.

    Credentials are owned by the OIDC issuer (e.g. the bundled Keycloak);
    pass both identity options to create or update a pre-linked REANA row.
    Omit both only when an external issuer's immutable subject is not yet
    available, then rerun this command with both options before login.
    """
    reana_user_characteristics = {
        "id_": id_,
        "email": email,
    }
    try:
        if bool(idp_issuer) != bool(idp_subject):
            raise ValueError(
                "--idp-issuer and --idp-subject must be provided together."
            )
        user_by_id = Session.query(User).filter_by(id_=id_).one_or_none()
        user_by_email = Session.query(User).filter_by(email=email).one_or_none()
        if user_by_id and user_by_email and user_by_id.id_ != user_by_email.id_:
            raise ValueError("Administrator id and email belong to different users.")
        user = user_by_id or user_by_email
        if not user:
            user = User(**reana_user_characteristics)
            create_user_workspace(user.get_user_workspace())
            Session.add(user)
        elif str(user.id_) != str(id_) or user.email != email:
            raise ValueError("Existing administrator id or email does not match.")

        if idp_issuer and idp_subject:
            link_user_identity(user, idp_issuer, idp_subject)
        Session.commit()
        click.echo(str(user.id_))
    except Exception as e:
        Session.rollback()
        click.echo("Something went wrong: {0}".format(e))
        sys.exit(1)


@reana_admin.command("link-user-identity")
@click.option(
    "-e",
    "--email",
    callback=_validate_email,
    required=True,
    help="Email of the existing REANA user to link.",
)
@click.option("--idp-issuer", required=True, help="Trusted OIDC issuer URL.")
@click.option("--idp-subject", required=True, help="Immutable OIDC subject.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate the single-user link without committing it.",
)
@with_appcontext
def link_user_identity_command(email, idp_issuer, idp_subject, dry_run):
    """Explicitly link one existing user during an OIDC migration."""
    try:
        user = Session.query(User).filter_by(email=email).one_or_none()
        if user is None:
            raise ValueError(f"No REANA user exists for '{email}'.")
        link_user_identity(user, idp_issuer, idp_subject)
        if dry_run:
            Session.rollback()
            click.echo(f"Would link {email} to {idp_issuer} / {idp_subject}.")
        else:
            Session.commit()
            click.echo(f"Linked {email} to {idp_issuer} / {idp_subject}.")
    except Exception as error:
        Session.rollback()
        raise click.ClickException(str(error)) from error


@reana_admin.command("gitlab-webhook-revoke")
@click.option(
    "--delete-secret",
    is_flag=True,
    help=(
        "Also delete the secret. Hooks already installed in GitLab stop working "
        "permanently and the user must recreate them through REANA."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be revoked without committing the change.",
)
@add_user_options
def gitlab_webhook_revoke(delete_secret: bool, dry_run: bool, user: Optional[User]):
    """Revoke a user's delegated GitLab webhook authorization immediately.

    Webhook secrets are delegated capabilities that expire on their own after
    ``REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME``. This command ends one now,
    without waiting for that bound, so that an offboarded or compromised
    account stops launching workflows from GitLab straight away.

    By default the secret is kept but de-authorized: the user can restore it
    from the REANA web interface, which re-checks their current identity
    provider entitlement. Use ``--delete-secret`` when the secret itself must
    be considered compromised.
    """
    if user is None:
        click.secho("Please specify the user with --email or --id.", fg="red", err=True)
        raise click.exceptions.Exit(1)
    if not user.gitlab_webhook_secret:
        click.echo(f"{user.email} has no GitLab webhook authorization to revoke.")
        return
    try:
        user.gitlab_webhook_secret_expires_at = None
        if delete_secret:
            user.gitlab_webhook_secret = None
        if dry_run:
            Session.rollback()
            click.echo(
                f"Would revoke the GitLab webhook authorization of {user.email}."
            )
            return
        Session.commit()
    except Exception as error:
        Session.rollback()
        raise click.ClickException(str(error)) from error
    click.secho(
        f"Revoked the GitLab webhook authorization of {user.email}.", fg="green"
    )
    if delete_secret:
        click.echo(
            "The secret was deleted. Existing GitLab hooks are permanently "
            "rejected; the user must re-enable each project in REANA."
        )
    else:
        click.echo(
            "The secret is preserved. The user can re-authorize it from the "
            "REANA web interface while they still hold the required role."
        )


@reana_admin.command("revoke-identity")
@click.option(
    "--delete-secret",
    is_flag=True,
    help=(
        "Also delete the GitLab webhook secret. Hooks already installed in "
        "GitLab stop working permanently and the user must recreate them "
        "through REANA."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be revoked without changing or deleting anything.",
)
@add_user_options
@with_appcontext
@click.pass_context
def revoke_identity(
    ctx, delete_secret: bool, dry_run: bool, user: Optional[User]
) -> None:
    """Revoke every REANA-side session and secret for one identity at once.

    Closes the user's open interactive sessions immediately, revokes their
    GitLab webhook authorization, and deletes their browser (BFF) sessions
    -- everything REANA itself can revoke without delay, in one command
    instead of three. Equivalent to running ``interactive-session-cleanup
    --email``, ``gitlab-webhook-revoke --email``, and a BFF-session
    revocation separately; see those commands for narrower standalone use
    (e.g. only closing sessions, or only a leaked-secret response).

    This does NOT remove the user's identity-provider role/entitlement, and
    cannot revoke a JWT access token already issued: REANA validates bearer
    tokens statelessly, so a live one keeps working until it expires
    regardless of anything this command does. For offboarding or a
    suspected compromise, remove the identity-provider role FIRST, then run
    this -- the same ordering ``gitlab-webhook-revoke``'s documentation
    already requires, now covering every REANA-side credential at once.
    """
    if user is None:
        click.secho("Please specify the user with --email or --id.", fg="red", err=True)
        raise click.exceptions.Exit(1)

    ctx.invoke(
        interactive_session_cleanup,
        days=None,
        dry_run=dry_run,
        email=user.email,
        id_=None,
    )
    ctx.invoke(
        gitlab_webhook_revoke,
        delete_secret=delete_secret,
        dry_run=dry_run,
        email=user.email,
        id_=None,
    )
    if user.idp_subject:
        count = delete_sessions_for_subject(
            user.idp_issuer, user.idp_subject, dry_run=dry_run
        )
        verb = "Would delete" if dry_run else "Deleted"
        click.echo(f"{verb} {count} browser session(s) for {user.email}.")
    else:
        click.echo(
            f"{user.email} has no linked identity-provider subject yet; "
            "no browser sessions to look up."
        )

    click.secho(
        "Remember to remove this user's identity-provider role separately "
        "-- a live access token keeps working until it expires regardless "
        "of anything this command does.",
        fg="yellow",
    )


@reana_admin.command("user-list", help="List users according to the search criteria.")
@click.option("--id", help="The id of the user.")
@click.option("-e", "--email", help="The email of the user.")
@click.option(
    "--json",
    "output_format",
    flag_value="json",
    default=None,
    help="Get output in JSON format.",
)
@click.pass_context
def list_users(ctx, id, email, output_format):
    """List users according to the search criteria."""
    try:
        response = _get_users(id, email)
        headers = ["id", "email", "idp_subject"]
        data = []
        for user in response:
            data.append(
                (
                    str(user.id_),
                    user.email,
                    str(user.idp_subject or ""),
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
@click.pass_context
def create_user(ctx, email):
    """Create a new user."""
    try:
        response = _create_user(email)
        headers = ["id", "email"]
        data = [(str(response.id_), response.email)]
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
@click.pass_context
def export_users(ctx):
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
@click.option(
    "-f",
    "--file",
    "file_",
    help="A CSV file containing a list of REANA users.",
    type=click.File(),
)
@click.pass_context
def import_users(ctx, file_):
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
def status_report(types, email):
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
@click.pass_context
def list_quota_usage(ctx, id, email, output_format, human_readable):
    """List quota usage of users."""
    try:
        response = _get_users(id, email)
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
    for resource in Session.query(Resource):
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
    msg, status_code, fatal = _set_quota_limit(
        limit=limit,
        resource_type=resource_type,
        resource_name=resource_name,
        emails=emails,
    )
    click.secho(
        msg,
        fg="green" if status_code == 200 else "red",
        err=False if status_code == 200 else True,
    )

    if fatal:
        sys.exit(1)


@reana_admin.command(
    "quota-set-period",
    help="Set periodic quota fields for a given user.",
)
@click.option("--id", "user_id", help="The id of the user.")
@click.option("-e", "--email", help="The email of the user.")
@click.option(
    "--resource",
    "resource_type",
    required=True,
    type=click.Choice(["cpu"]),
    help="Specify quota resource. Only cpu is currently supported.",
)
@click.option(
    "--quota-period-months",
    type=int,
    default=None,
    callback=_unset_if_option_omitted,
    help="Length of quota period in months.",
)
@click.option(
    "--quota-period-start-at",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]),
    default=None,
    callback=_unset_if_option_omitted,
    help="Current active quota period start datetime.",
)
@click.pass_context
def set_quota_period(
    ctx,
    user_id,
    email,
    resource_type,
    quota_period_months,
    quota_period_start_at,
):
    """Set periodic quota fields for one user."""
    if int(bool(user_id)) + int(bool(email)) != 1:
        click.secho(
            "ERROR: Exactly one of `user_id` or `email` must be provided.",
            fg="red",
            err=True,
        )
        sys.exit(1)

    msg, status_code, fatal = _set_quota_period(
        resource_type=resource_type,
        quota_period_months=quota_period_months,
        quota_period_start_at=quota_period_start_at,
        user_id=user_id,
        email=email,
    )

    click.secho(
        msg or "Periodic quota fields updated successfully.",
        fg="green" if status_code == 200 else "red",
        err=False if status_code == 200 else True,
    )

    if fatal:
        sys.exit(1)


@reana_admin.command(
    "quota-set-default-limits",
    help="""Set default quota limits for users who do not have any custom limits
         defined.

    Note that any previously set user limits, either via old defaults or via
    custom settings, will be kept during the upgrade, and won't be automatically
    updated to match the new default limit value.""",
)
@click.pass_context
def set_default_quota_limit(ctx):
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

    resources = Session.query(Resource).all()

    for user in users_without_quota_limits:
        for resource in resources:
            user_resource = (
                Session.query(UserResource)
                .filter_by(user_id=user.id_, resource_id=resource.id_)
                .first()
            )

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
def retention_rules_apply(  # noqa: C901
    dry_run: bool,
    force_date: Optional[datetime.datetime],
    yes_i_am_sure: bool,
    user: Optional[User],
    workflow: Optional[Workflow],
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

    candidate_rules = Session.query(WorkspaceRetentionRule)
    if workflow:
        candidate_rules = candidate_rules.filter_by(workflow_id=workflow.id_)
    elif user:
        candidate_rules = candidate_rules.join(Workflow).filter(
            Workflow.owner_id == user.id_
        )

    active_rules = candidate_rules.filter(
        WorkspaceRetentionRule.status == WorkspaceRetentionRuleStatus.active,
        WorkspaceRetentionRule.apply_on < current_time,
    )
    click.echo("Fetching all the pending rules")
    pending_rules = candidate_rules.filter(
        WorkspaceRetentionRule.status == WorkspaceRetentionRuleStatus.pending
    )
    rules_to_apply = pending_rules.union(active_rules).all()

    if not rules_to_apply:
        click.echo("No rules to be applied!")

    rules_by_workspace = {}
    for rule in rules_to_apply:
        rules_by_workspace.setdefault(rule.workflow.workspace_path, []).append(rule.id_)

    for workspace_path, rule_ids in rules_by_workspace.items():
        try:
            with workspace_mutation_lock(workspace_path):
                # Refresh decisions while holding the same lock used by API
                # workspace mutations. This closes the restart/delete
                # check-then-act window and lets stale pending rules retry.
                rules = (
                    Session.query(WorkspaceRetentionRule)
                    .filter(
                        WorkspaceRetentionRule.id_.in_(rule_ids),
                        (
                            WorkspaceRetentionRule.status
                            == WorkspaceRetentionRuleStatus.pending
                        )
                        | (
                            (
                                WorkspaceRetentionRule.status
                                == WorkspaceRetentionRuleStatus.active
                            )
                            & (WorkspaceRetentionRule.apply_on < current_time)
                        ),
                    )
                    .all()
                )
                if not dry_run:
                    for rule in rules:
                        if rule.status == WorkspaceRetentionRuleStatus.active:
                            rule.status = WorkspaceRetentionRuleStatus.pending
                    Session.commit()

                for rule in rules:
                    if not Path(workspace_path).exists():
                        click.secho(
                            f"Workspace {workspace_path} of rule {rule.id_} does not "
                            "exist, setting the status to `applied`",
                            fg="red",
                        )
                        if not dry_run:
                            rule.status = WorkspaceRetentionRuleStatus.applied
                        continue

                    next_status = WorkspaceRetentionRuleStatus.active
                    try:
                        RetentionRuleDeleter(rule).apply_rule(dry_run)
                        next_status = WorkspaceRetentionRuleStatus.applied
                    except Exception as error:
                        click.secho(
                            f"Error while applying rule {rule.id_}: {error}", fg="red"
                        )
                        logging.debug(error, exc_info=True)
                    if not dry_run:
                        click.echo(
                            f"Setting the status of rule {rule.id_} to "
                            f"`{next_status.name}`"
                        )
                        rule.status = next_status
                if not dry_run:
                    Session.commit()
        except WorkspaceMutationConflict:
            Session.rollback()
            click.echo(
                f"Workspace {workspace_path} is currently being modified; "
                "retention rules will be retried later."
            )
        except WorkspaceMutationUnavailable:
            Session.rollback()
            click.secho(
                f"Could not serialize retention rules for workspace {workspace_path}; "
                "they will be retried later.",
                fg="red",
            )
            logging.exception("Workspace mutation locking is unavailable.")


@reana_admin.command()
@add_workflow_option(required=True)
@click.option(
    "--days",
    "-d",
    help="Number of days to extend the rules.",
    required=True,
    type=click.IntRange(min=0),
)
def retention_rules_extend(workflow: Optional[Workflow], days: int) -> None:
    """Extend active retentions rules."""
    click.echo("Fetching all the active rules")
    active_rules = (
        Session.query(WorkspaceRetentionRule)
        .filter(
            WorkspaceRetentionRule.status == WorkspaceRetentionRuleStatus.active,
            WorkspaceRetentionRule.workflow_id == workflow.id_,
        )
        .all()
    )

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
def check_workflows(
    date_start: datetime.datetime,
    date_end: Optional[datetime.datetime],
    show_all: bool,
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
    type=click.IntRange(min=0),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show which interactive sessions would be closed, without closing them. [default=False]",
)
@add_user_options
def interactive_session_cleanup(  # noqa: C901
    days: Optional[int], dry_run: bool, user: Optional[User]
) -> None:
    """Close inactive interactive sessions, or one user's immediately.

    Without ``--email``/``--id``, closes sessions inactive for more than
    ``--days``. With ``--email``/``--id``, ignores ``--days`` and closes
    every one of that user's active sessions right away -- this is REANA's
    way to make interactive-session revocation immediate (rather than
    bounded only by the configured inactivity cleanup) when offboarding a
    user or responding to a suspected account compromise, matching
    ``gitlab-webhook-revoke``'s role in the webhook revocation lifecycle.
    """
    if user is None and days is None:
        click.secho(
            "Please specify --days for inactivity-based cleanup, "
            "or --email/--id to immediately close one user's sessions.",
            fg="red",
            err=True,
        )
        raise click.exceptions.Exit(1)
    if user is not None:
        click.echo(f"Starting to close all interactive sessions for {user.email}..")
    else:
        click.echo(
            f"Starting to close interactive sessions running longer than {days} days.."
        )
    click.echo("Fetching interactive session pods..")
    label_selector = "reana_workflow_mode=session"
    if user is not None:
        label_selector += f",user-uuid={user.id_}"
    try:
        pods = current_k8s_corev1_api_client.list_namespaced_pod(
            namespace=REANA_RUNTIME_KUBERNETES_NAMESPACE,
            label_selector=label_selector,
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
            user_id = pod.metadata.labels["user-uuid"]
            workflow = (
                Session.query(Workflow)
                .filter_by(id_=workflow_id, owner_id=user_id)
                .one_or_none()
            )
            if workflow is None:
                raise ValueError("matching workflow not found")
            matching_sessions = [
                session
                for session in workflow.sessions
                if session.status != RunStatus.deleted
                and (
                    pod_name == session.name or pod_name.startswith(f"{session.name}-")
                )
            ]
            if len(matching_sessions) != 1:
                raise ValueError("matching interactive session not found")
            matching_session = matching_sessions[0]
        except Exception as e:
            click.secho(
                f"Couldn't parse user details from '{pod_name}' session metadata: {e}",
                fg="red",
                err=True,
            )
            logging.debug(e, exc_info=True)
            continue

        if user is not None:
            # Immediate revocation: close regardless of inactivity duration,
            # so this isn't gated by the session's own (self-reported, and
            # therefore potentially compromised-account-controlled) status.
            if dry_run:
                click.echo(f"Interactive session '{pod_name}' would be closed.")
                continue
            try:
                current_rwc_api_client.api.close_interactive_session(
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
            continue

        # The inactivity check needs to query the live notebook's own status
        # endpoint, which requires the session's token -- unlike immediate
        # revocation above, which only needs the workflow/user identity.
        # Sessions created before the session-token feature shipped have
        # session_secret == None and can't be inactivity-checked this way.
        token = matching_session.session_secret
        if not token:
            click.secho(
                f"Interactive session '{pod_name}' has no session token, "
                "cannot check its inactivity status.",
                fg="red",
                err=True,
            )
            continue

        try:
            session_status = requests.get(
                f"http://reana-run-session-{workflow_id}.{REANA_RUNTIME_KUBERNETES_NAMESPACE}:8081/{workflow_id}/api/status",
                headers={"Authorization": f"token {token}"},
                timeout=10,
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
        )
        duration = datetime.datetime.now(datetime.UTC) - last_activity
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
