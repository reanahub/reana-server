# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server utils."""

import base64
import csv
import io
import logging
import os
import pathlib
import shutil
import sys
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

from uuid import UUID, uuid4

import click
import yaml
from marshmallow.exceptions import ValidationError
from marshmallow.validate import Email
from reana_commons.config import REANAConfig, REANA_WORKFLOW_UMASK, SHARED_VOLUME_PATH
from reana_commons.errors import (
    REANAQuotaExceededError,
    REANAValidationError,
)
from reana_commons.utils import get_dask_component_name, get_quota_resource_usage
from reana_commons.yadage import yadage_load_from_workspace
from reana_db.database import Session
from reana_db.models import (
    ResourceType,
    ResourceUnit,
    RunStatus,
    Service,
    ServiceStatus,
    ServiceType,
    User,
    UserResource,
    Workflow,
    Resource,
)
from reana_db.utils import (
    get_current_quota_period_start_at,
    get_default_quota_resource,
)
from sqlalchemy.exc import (
    IntegrityError,
    InvalidRequestError,
    SQLAlchemyError,
    StatementError,
)

from reana_server.api_client import current_workflow_submission_publisher
from reana_server.complexity import (
    get_workflow_min_job_memory,
    estimate_complexity,
    validate_job_memory_limits,
)
from reana_server.config import (
    ADMIN_USER_ID,
    REANA_HOSTNAME,
    REANA_URL,
    REANA_WORKFLOW_SCHEDULING_POLICY,
    REANA_WORKFLOW_SCHEDULING_POLICIES,
    REANA_QUOTAS_DOCS_URL,
    WORKSPACE_RETENTION_PERIOD,
    DEFAULT_WORKSPACE_RETENTION_RULE,
)
from reana_server.gitlab_client import (
    GitLabClient,
    GitLabClientException,
)
from reana_server.validation import validate_retention_rule, validate_workflow


def is_uuid_v4(uuid_or_name):
    """Check if given string is a valid UUIDv4."""
    # Based on https://gist.github.com/ShawnMilo/7777304
    try:
        uuid = UUID(uuid_or_name, version=4)
    except Exception:
        return False

    return uuid.hex == uuid_or_name.replace("-", "")


def create_user_workspace(user_workspace_path):
    """Create user workspace directory."""
    if not os.path.isdir(user_workspace_path):
        os.umask(REANA_WORKFLOW_UMASK)
        os.makedirs(user_workspace_path, exist_ok=True)


def get_fetched_workflows_dir(user_id: str) -> str:
    """Return temporary directory for fetching workflow files."""
    tmpdir = os.path.join(
        SHARED_VOLUME_PATH, "users", user_id, "workflowsfetched", str(uuid4())
    )
    create_user_workspace(tmpdir)
    return tmpdir


def get_quota_excess_message(user) -> str:
    """Return detailed quota excess message.

    :param user: User whose quota needs to be checked.
    """
    quota = user.get_quota_usage()
    message = "User quota exceeded.\n"
    for resource_type, resource in quota.items():
        limit = resource.get("limit", {}).get("raw", 0)
        usage = resource.get("usage", {}).get("raw", 0)
        if 0 < limit <= usage:
            resource_usage, _ = get_quota_resource_usage(resource, "human_readable")
            message += f"Resource: {resource_type}, usage: {resource_usage}\n"

    message += f"Please see: {REANA_QUOTAS_DOCS_URL}"
    return message


def prevent_disk_quota_excess(user, bytes_to_sum: Optional[int], action=Optional[str]):
    """
    Prevent potential disk quota excess.

    E.g. when uploading big files or launching new workflows.

    :param user: User whose quota needs to be checked.
    :param bytes_to_sum: Bytes to be added to the user's quota.
    :param action: Optional action description used for custom error messages.
    """
    disk_resource = get_default_quota_resource(ResourceType.disk.name)
    user_resource = (
        Session.query(UserResource)
        .filter_by(user_id=user.id_, resource_id=disk_resource.id_)
        .first()
    )

    if bytes_to_sum is None:
        bytes_to_sum = 0

    if 0 < user_resource.quota_limit < user_resource.quota_used + bytes_to_sum:
        human_readable_limit = ResourceUnit.human_readable_unit(
            ResourceUnit.bytes_, user_resource.quota_limit
        )
        if not action:
            action = "This action"
        raise REANAQuotaExceededError(
            f"{action} would exceed the disk quota limit "
            f"({human_readable_limit}). Aborting. Please see: {REANA_QUOTAS_DOCS_URL}"
        )


def remove_fetched_workflows_dir(tmpdir: str) -> None:
    """Remove temporary directory used for fetching workflow files."""
    if tmpdir and os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir)


def mv_workflow_files(source: str, target: str) -> None:
    """Move files from one directory to another."""
    for entry in os.listdir(source):
        shutil.move(os.path.join(source, entry), target)


# FIXME: use `is_relative_to` from the standard library when moving to Python 3.9
def is_relative_to(path: pathlib.Path, base: pathlib.Path) -> bool:
    """Check whether `path` is contained inside `base`."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def filter_input_files(workspace: Union[str, pathlib.Path], reana_spec: Dict) -> None:
    """Delete the files and directories not specified as inputs in the specification.

    :param workspace: Path to the directory containing the files to be filtered.
    :param reana_spec: REANA specification used to decide which files to keep.
    """
    inputs = reana_spec.get("inputs", {})
    files = inputs.get("files", [])
    directories = inputs.get("directories", [])

    if isinstance(workspace, str):
        workspace = pathlib.Path(workspace)

    for file in files:
        file_path = workspace / file
        if not file_path.exists():
            raise REANAValidationError(f"Input file not found: {file}")

    for directory in directories:
        directory_path = workspace / directory
        if not directory_path.exists():
            raise REANAValidationError(f"Input directory not found: {directory}")

    paths = [pathlib.Path(path) for path in files + directories]
    filtered = workspace / f"filtered-{uuid4()}"
    filtered.mkdir()

    # Move input files to the temporary `filtered` directory
    for path in paths:
        full_source_path = workspace / path
        full_target_path = filtered / path
        # Create target directory if it does not exist
        full_target_path.parent.mkdir(parents=True, exist_ok=True)
        full_source_path.replace(full_target_path)

    # Delete remaining files in the workspace
    for path in workspace.iterdir():
        if path == filtered:
            continue
        if path.is_file() or path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path)

    # Move the contents of the temporary `filtered` directory to the workspace
    for path in filtered.iterdir():
        path.replace(workspace / path.name)

    # Remove temporary `filtered` directory
    filtered.rmdir()


def publish_workflow_submission(workflow, user_id, parameters):
    """Publish workflow submission."""
    from reana_server.status import NodesStatus

    Workflow.update_workflow_status(Session, workflow.id_, RunStatus.queued)

    scheduling_policy = REANA_WORKFLOW_SCHEDULING_POLICY
    if scheduling_policy not in REANA_WORKFLOW_SCHEDULING_POLICIES:
        raise ValueError(
            'Workflow scheduling policy "{0}" is not valid.'.format(scheduling_policy)
        )

    # No need to estimate the complexity for "fifo" strategy
    if scheduling_policy == "fifo":
        workflow_priority = 0
        workflow_min_job_memory = 0
    else:
        total_cluster_memory = NodesStatus().get_total_memory()
        complexity = _calculate_complexity(workflow)
        workflow_priority = workflow.get_priority(total_cluster_memory)
        workflow_min_job_memory = get_workflow_min_job_memory(complexity)
        validate_job_memory_limits(complexity)
    current_workflow_submission_publisher.publish_workflow_submission(
        user_id=str(user_id),
        workflow_id_or_name=str(workflow.id_),
        parameters=parameters,
        priority=workflow_priority,
        min_job_memory=workflow_min_job_memory,
    )


def _calculate_complexity(workflow):
    """Place workflow in queue and calculate and set its complexity."""
    complexity = estimate_complexity(workflow.type_, workflow.reana_specification)
    workflow.complexity = complexity
    Session.commit()
    return complexity


def serialize_utc_datetime(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a naive UTC datetime using an RFC 3339 UTC designator."""
    return dt.isoformat() + "Z" if dt else None


def _load_and_save_yadage_spec(workflow: Workflow, operational_options: Dict):
    """Load and save in DB the Yadage workflow specification."""
    operational_options.update({"accept_metadir": True})
    toplevel = operational_options.get("toplevel", "")
    workflow.reana_specification = yadage_load_from_workspace(
        workflow.workspace_path,
        workflow.reana_specification,
        toplevel,
    )
    Session.commit()


def _get_admin_user_or_raise(*, requested_via: str) -> User:
    """Return the configured admin user or raise on server misconfiguration."""
    admin = Session.query(User).filter_by(id_=ADMIN_USER_ID).one_or_none()
    if not admin:
        logging.error(
            "ADMIN_USER_ID %s does not resolve to a known user; refusing %s.",
            ADMIN_USER_ID,
            requested_via,
        )
        raise RuntimeError("Server misconfiguration.")
    return admin


def _get_users(_id, email):
    """Return all users matching search criteria."""
    search_criteria = dict()
    if _id:
        search_criteria["id_"] = _id
    if email:
        search_criteria["email"] = email
    return Session.query(User).filter_by(**search_criteria).all()


def _create_user(email):
    """Create user with the provided email."""
    try:
        user = User(email=email)
        Session.add(user)
        Session.commit()
    except (InvalidRequestError, IntegrityError):
        Session.rollback()
        raise ValueError("Could not create user, " "possible constraint violation")
    return user


def _export_users():
    """Export all users in database as csv."""
    csv_file_obj = io.StringIO()
    csv_writer = csv.writer(csv_file_obj, dialect="unix")
    for user in Session.query(User).all():
        csv_writer.writerow(
            [user.id_, user.email, user.username, user.full_name]
        )
    return csv_file_obj


def _import_users(users_csv_file):
    """Import list of users to database.

    :param users_csv_file: CSV file object containing a list of users.
    :type users_csv_file: _io.TextIOWrapper
    """
    csv_reader = csv.reader(users_csv_file)
    for row in csv_reader:
        user = User(
            id_=row[0],
            email=row[1],
            username=row[2],
            full_name=row[3],
        )
        Session.add(user)
    Session.commit()



def _get_reana_yaml_from_gitlab(webhook_data, user_id):
    reana_yaml = "reana.yaml"
    if webhook_data["object_kind"] == "push":
        branch = webhook_data["project"]["default_branch"]
        commit_sha = webhook_data["checkout_sha"]
    elif webhook_data["object_kind"] == "merge_request":
        branch = webhook_data["object_attributes"]["source_branch"]
        commit_sha = webhook_data["object_attributes"]["last_commit"]["id"]
    project_id = webhook_data["project"]["id"]
    gitlab_client = GitLabClient.from_k8s_secret(user_id)
    yaml_file = gitlab_client.get_file(project_id, reana_yaml, branch).content
    return (
        yaml.safe_load(yaml_file),
        webhook_data["project"]["path_with_namespace"],
        webhook_data["project"]["name"],
        branch,
        commit_sha,
    )


def _fail_gitlab_commit_build_status(
    user: User, git_repo: str, git_ref: str, description: str
):
    """Send request to Gitlab to fail commit build status.

    HTTP errors will be ignored.
    """
    state = "failed"
    try:
        gitlab_client = GitLabClient.from_k8s_secret(user.id_)
        gitlab_client.set_commit_build_status(git_repo, git_ref, state, description)
    except GitLabClientException as e:
        logging.warn(f"Could not set commit build status: {e}")


def _format_gitlab_secrets(gitlab_user, access_token):
    return {
        "gitlab_access_token": {
            "value": base64.b64encode(access_token.encode("utf-8")).decode("utf-8"),
            "type": "env",
        },
        "gitlab_user": {
            "value": base64.b64encode(gitlab_user["username"].encode("utf-8")).decode(
                "utf-8"
            ),
            "type": "env",
        },
    }


def _get_gitlab_hook_id(project_id, gitlab_client: GitLabClient):
    """Return REANA hook id from a GitLab project if it is connected.

    By checking its webhooks and comparing them to REANA ones.

    :param project_id: Project id on GitLab.
    :param gitlab_client: GitLab client.
    """
    create_workflow_url = f"{REANA_URL}/api/workflows"
    try:
        for hook in gitlab_client.get_all_webhooks(project_id):
            if hook["url"] and hook["url"] == create_workflow_url:
                return hook["id"]
    except GitLabClientException as e:
        logging.warn(f"GitLab hook request failed: {e}")
    return None


class RequestStreamWithLen(object):
    """Wrap ``request.stream`` object to have ``__len__`` attribute.

    Users can upload files to REANA through REANA-Server (RS). RS passes then
    the content of the file uploads to the next REANA component,
    REANA-Workflow-Controller (RWC).

    In order for this operation to be efficient we read the user stream upload
    using ``werkzeug`` streams through ``request.stream``. Then, to pass this
    stream to RWC without creating memory leaks we stream upload the
    ``request.stream`` content using the Requests library. However, the
    Request library is not aware of how the size of the stream is represented
    in ``werkzeug`` (``limit`` attribute), Requests only understands
    ``len(stream)`` or ``stream.len``, see more here
    https://github.com/psf/requests/blob/3e7d0a873f838e0001f7ac69b1987147128a7b5f/requests/utils.py#L108-L166

    This class provides the necessary attributes for compatibility with
    Requests stream upload.
    """

    def __init__(self, limitedstream):
        """Wrap the stream to have ``len``."""
        self.limitedstream = limitedstream

    def read(self, *args, **kwargs):
        """Expose ``request.stream``s read method."""
        return self.limitedstream.read(*args, **kwargs)

    def __len__(self):
        """Expose the length of the ``request.stream``."""
        if not hasattr(self.limitedstream, "limit"):
            return 0
        return self.limitedstream.limit


def get_workspace_retention_rules(
    retention_days: Optional[Dict[str, int]],
) -> List[Dict[str, any]]:
    """Validate and return a list of retention rules.

    :raises reana_commons.errors.REANAValidationError: in case one of the rules is not valid
    """
    retention_rules = []
    if retention_days:
        for rule, days in retention_days.items():
            validate_retention_rule(rule, days)
            retention_rules.append({"workspace_files": rule, "retention_days": days})

    default_retention_rules_are_not_disabled = WORKSPACE_RETENTION_PERIOD is not None
    default_retention_rule_is_not_present = not any(
        rule["workspace_files"] == DEFAULT_WORKSPACE_RETENTION_RULE
        for rule in retention_rules
    )

    if (
        default_retention_rule_is_not_present
        and default_retention_rules_are_not_disabled
    ):
        retention_rules.append(
            {
                "workspace_files": DEFAULT_WORKSPACE_RETENTION_RULE,
                "retention_days": WORKSPACE_RETENTION_PERIOD,
            }
        )
    return retention_rules


def workflow_uses_dask(reana_specification: Dict) -> bool:
    """Check whether a workflow specification requests a Dask service."""
    return bool(
        reana_specification.get("workflow", {}).get("resources", {}).get("dask")
    )


def build_dask_service(workflow: Workflow) -> Service:
    """Build the Dask service DB row for a workflow."""
    return Service(
        name=get_dask_component_name(workflow.id_, "database_model_service"),
        uri=f"{REANA_URL}/{workflow.id_}/dashboard/status",
        type_=ServiceType.dask,
        status=ServiceStatus.created,
    )


def ensure_dask_service(workflow: Workflow) -> bool:
    """Ensure that a Dask workflow has a matching service DB row."""
    if not workflow_uses_dask(workflow.reana_specification):
        return False

    service_name = get_dask_component_name(workflow.id_, "database_model_service")
    if any(s.name == service_name for s in workflow.services):
        return False

    workflow.services.append(build_dask_service(workflow))
    return True


def clone_workflow(workflow, reana_spec, restart_type):
    """Create a copy of workflow in DB for restarting."""
    reana_specification = reana_spec or workflow.reana_specification
    validate_workflow(reana_specification, input_parameters={})

    retention_days = reana_specification.get("workspace", {}).get("retention_days")
    retention_rules = get_workspace_retention_rules(retention_days)
    try:
        cloned_workflow = Workflow(
            id_=str(uuid4()),
            name=workflow.name,
            owner_id=workflow.owner_id,
            reana_specification=reana_spec or workflow.reana_specification,
            type_=restart_type or workflow.type_,
            logs="",
            workspace_path=workflow.workspace_path,
            restart=True,
            run_number=workflow.run_number,
        )
        if workflow_uses_dask(reana_specification):
            cloned_workflow.services.append(build_dask_service(cloned_workflow))
        Session.add(cloned_workflow)
        Session.object_session(cloned_workflow).commit()
        workflow.inactivate_workspace_retention_rules()
        cloned_workflow.set_workspace_retention_rules(retention_rules)
        return cloned_workflow
    except SQLAlchemyError as e:
        message = "Database connection failed, please retry."
        logging.error(
            f"Error while creating {cloned_workflow.id_}: {message}\n{e}", exc_info=True
        )


def _get_user_by_criteria(id_: Optional[str], email: Optional[str]) -> Optional[User]:
    """Get user filtering first by id, then by email."""
    criteria = dict()
    if id_:
        criteria["id_"] = id_
    elif email:
        criteria["email"] = email
    if not criteria:
        return None
    try:
        return Session.query(User).filter_by(**criteria).one_or_none()
    except StatementError as e:
        print(e)
        return None


def is_valid_email(value: str) -> bool:  # noqa: D103
    try:
        validator = Email()
        validator(value)
        return True
    except ValidationError:
        return False


def _validate_email(ctx, param, value):
    """Validate email callback for click CLI option."""
    if not is_valid_email(value):
        click.secho("ERROR: Invalid email format", fg="red")
        sys.exit(1)
    return value


def _set_quota_limit(
    limit: int,
    resource_type: Optional[str] = None,
    resource_name: Optional[str] = None,
    emails: Optional[list[str]] = None,
    user_ids: Optional[list[str]] = None,
) -> tuple[Optional[str], int, bool]:
    """
    Set a quota limit for the given users and resource type.

    :param limit: Value of new limit.
    :param resource_type: Type of the resource.
    :param resource_name: Name of the resource.
    :param emails: List of user emails.
    :param user_ids: List of user IDs.
    :return: Tuple message, status code and whether the error is fatal.
    """
    if (emails is None and user_ids is None) or (emails and user_ids):
        return (
            "ERROR: Exactly one of `emails` or `user_ids` must be provided.",
            400,
            True,
        )

    if emails is not None and len(emails) == 0:
        return "ERROR: `emails` must not be empty.", 400, True

    if user_ids is not None and len(user_ids) == 0:
        return "ERROR: `user_ids` must not be empty.", 400, True

    if (not resource_type and not resource_name) or (resource_type and resource_name):
        return (
            "ERROR: Exactly one of `resource_type` or `resource_name` must be provided.",
            400,
            True,
        )

    if limit < 0:
        return "ERROR: Provided `limit` must be a positive number.", 400, True

    if user_ids and any(is_valid_email(uid) for uid in user_ids):
        return (
            f"ERROR: One or more `user_ids` look like an email: {', '.join(user_ids)}",
            400,
            True,
        )

    try:
        users = (
            [_get_user_by_criteria(None, email) for email in emails]
            if emails
            else [_get_user_by_criteria(uid, None) for uid in user_ids]
        )

        if any(user is None for user in users):
            users_not_found = [
                id_ for id_, user in zip(emails or user_ids, users) if user is None
            ]
            return (
                f"ERROR: The following users do not exist: {', '.join(users_not_found)}",
                404,
                True,
            )

        # Get resource by name or type
        resource = None
        if resource_name:
            resource = (
                Session.query(Resource).filter_by(name=resource_name).one_or_none()
            )
        elif resource_type in ResourceType._member_names_:
            available_resources = (
                Session.query(Resource).filter_by(type_=resource_type).all()
            )
            if len(available_resources) > 1:
                return (
                    f"ERROR: There are more than one `{resource_type}` resource. Please provide resource name to specify the exact resource.",
                    400,
                    True,
                )
            elif len(available_resources) == 1:
                resource = available_resources[0]

        if not resource:
            # Resource was not found
            available_resources = [
                (resource.type_.name if resource_type else resource.name)
                for resource in Session.query(Resource)
            ]

            return (
                f"ERROR: Provided resource '{resource_name or resource_type}' does not exist. Available resources: {', '.join(available_resources)}",
                400,
                True,
            )

        for user in users:
            user_resource = (
                Session.query(UserResource)
                .filter_by(user=user, resource=resource)
                .one_or_none()
            )

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
        return (
            f"Quota limit {limit} for '{resource.type_.name} ({resource.name})' successfully set to user(s): {', '.join([user.email if emails else str(user.id_) for user in users])}.",
            200,
            False,
        )
    except Exception as e:
        logging.debug(traceback.format_exc())
        logging.debug(str(e))
        return "Error setting quota limit: \n{}".format(str(e)), 500, False


_UNSET = object()


def _set_quota_period(
    *,
    resource_type: str,
    quota_period_months: Optional[int] = _UNSET,
    quota_period_start_at: Optional[datetime] = _UNSET,
    email: Optional[str] = None,
    user_id: Optional[str] = None,
) -> tuple[Optional[str], int, bool]:
    """Set periodic quota fields for one user.

    Only CPU resources are supported for now.
    """
    if resource_type != ResourceType.cpu.name:
        return "Periodic quota is currently supported only for CPU.", 400, True

    if int(bool(user_id)) + int(bool(email)) != 1:
        return "Exactly one of `user_id` or `email` must be provided.", 400, True

    try:
        user = _get_user_by_criteria(user_id, email)
        if not user:
            return "User not found.", 404, True

        cpu_resource = get_default_quota_resource(ResourceType.cpu.name)
        user_resource = (
            Session.query(UserResource)
            .filter_by(
                user_id=user.id_,
                resource_id=cpu_resource.id_,
            )
            .one_or_none()
        )

        if not user_resource:
            return "User CPU quota resource not found.", 404, True

        if (
            quota_period_months is not _UNSET
            and quota_period_months is not None
            and quota_period_months <= 0
        ):
            return "`quota_period_months` must be a positive integer.", 400, True

        effective_quota_period_months = (
            quota_period_months
            if quota_period_months is not _UNSET
            else user_resource.quota_period_months
        )

        if (
            quota_period_start_at is not _UNSET
            and effective_quota_period_months is None
        ):
            return (
                "Cannot set `quota_period_start_at` for a user without periodic "
                "quota cadence. Set `quota_period_months` first.",
                400,
                True,
            )

        if quota_period_months is not _UNSET:
            user_resource.quota_period_months = quota_period_months
            if quota_period_months is None:
                user_resource.quota_period_start_at = None
        if quota_period_start_at is _UNSET and not user_resource.quota_period_start_at:
            quota_period_start_at = get_current_quota_period_start_at(
                reference_start_at=user.created,
                quota_period_months=effective_quota_period_months,
            )

        if quota_period_start_at is not _UNSET:
            user_resource.quota_period_start_at = quota_period_start_at

        Session.commit()
        return None, 200, False
    except Exception as e:
        Session.rollback()
        logging.debug(traceback.format_exc())
        logging.debug(str(e))
        return "Error setting quota period: \n{}".format(str(e)), 500, False
