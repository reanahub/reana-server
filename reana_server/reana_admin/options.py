# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""Common options for the REANA administrator command line tool."""

import functools
import os
import sys

import click
from reana_db.models import Workflow

from reana_server.utils import (
    _get_user_by_criteria,
    _validate_admin_access_token,
    is_uuid_v4,
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
        try:
            _validate_admin_access_token(kwargs.get("admin_access_token"))
        except ValueError as e:
            click.echo(
                click.style(str(e), fg="red"),
                err=True,
            )
            sys.exit(1)
        return func(*args, **kwargs)

    return wrapper


def add_user_options(func):
    """Add options to get an user by email or id."""

    @click.option("-e", "--email", help="The email of the user.")
    @click.option("--id", "id_", help="The id of the user.")
    @functools.wraps(func)
    def wrapper(*args, email, id_, **kwargs):
        if id_ is not None and email is not None:
            click.secho("Cannot provide --email and --id at the same time.", fg="red")
            sys.exit(1)
        user = None
        if id_ is not None or email is not None:
            user = _get_user_by_criteria(id_, email)
            if not user:
                click.secho("User not found.", fg="red")
                sys.exit(1)
        func(*args, user=user, **kwargs)

    return wrapper


def add_workflow_option(**attrs):
    """Add options to get a workflow by its UUID."""

    def decorator(func):
        @click.option(
            "-w", "--workflow", "workflow_uuid", help="The id of the workflow.", **attrs
        )
        @functools.wraps(func)
        def wrapper(*args, workflow_uuid, **kwargs):
            workflow = None
            if workflow_uuid is not None:
                if not is_uuid_v4(workflow_uuid):
                    click.secho("Invalid workflow UUID.", fg="red")
                    sys.exit(1)
                workflow = Workflow.query.filter(Workflow.id_ == workflow_uuid).first()
                if not workflow:
                    click.secho("Workflow not found.", fg="red")
                    sys.exit(1)
            func(*args, workflow=workflow, **kwargs)

        return wrapper

    return decorator
