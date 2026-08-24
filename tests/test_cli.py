# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021, 2022, 2023, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test command line application."""

import csv
import datetime
import io
import pathlib
import uuid
from unittest.mock import ANY, MagicMock, Mock, patch

import click
import pytest
from click.testing import CliRunner
from reana_commons.testing import make_mock_api_client
from reana_db.models import (
    InteractiveSession,
    Resource,
    ResourceType,
    RunStatus,
    User,
    UserResource,
    Workflow,
    WorkspaceRetentionRuleStatus,
    generate_uuid,
)
from reana_server.api_client import WorkflowSubmissionPublisher
from reana_server.reana_admin import reana_admin
from reana_server.reana_admin.check_workflows import check_workspaces
from reana_server.reana_admin.cli import RetentionRuleDeleter
from reana_server.reana_admin.consumer import MessageConsumer
from reana_server.workspace_mutations import WorkspaceMutationConflict
from reana_server.reana_admin.options import (
    add_user_options,
    add_workflow_option,
)
import reana_server.auth.sessions as sessions_module
from reana_server.auth.errors import InvalidTokenError
from reana_server.decorators import _get_user_from_gitlab_secret
from reana_server.utils import naive_utcnow


def test_admin_user_options_exit_cleanly_on_invalid_selection():
    """Guarded user-option failures use Click's exit machinery."""

    @click.command()
    @add_user_options
    def command(user):
        click.echo(user.email if user else "all users")

    runner = CliRunner()
    conflicting = runner.invoke(
        command, ["--id", str(uuid.uuid4()), "--email", "user@example.org"]
    )
    assert conflicting.exit_code == 1
    assert conflicting.output == ("Cannot provide --email and --id at the same time.\n")

    with patch(
        "reana_server.reana_admin.options._get_user_by_criteria",
        return_value=None,
    ):
        missing = runner.invoke(command, ["--email", "missing@example.org"])
    assert missing.exit_code == 1
    assert missing.output == "User not found.\n"


def test_admin_workflow_option_exits_cleanly_on_invalid_selection():
    """Guarded workflow-option failures use Click's exit machinery."""

    @click.command()
    @add_workflow_option()
    def command(workflow):
        click.echo(workflow.id_ if workflow else "all workflows")

    runner = CliRunner()
    invalid = runner.invoke(command, ["--workflow", "not-a-uuid"])
    assert invalid.exit_code == 1
    assert invalid.output == "Invalid workflow UUID.\n"

    query = Mock()
    query.filter.return_value.first.return_value = None
    with patch("reana_server.reana_admin.options.Session.query", return_value=query):
        missing = runner.invoke(command, ["--workflow", str(uuid.uuid4())])
    assert missing.exit_code == 1
    assert missing.output == "Workflow not found.\n"


def test_export_users(user0):
    """Test exporting all users as csv."""
    runner = CliRunner()
    expected_csv_file = io.StringIO()
    csv_writer = csv.writer(expected_csv_file, dialect="unix")
    csv_writer.writerow(
        [
            user0.id_,
            user0.email,
            user0.username,
            user0.full_name,
        ]
    )
    result = runner.invoke(reana_admin, ["user-export"])
    assert result.output == expected_csv_file.getvalue()


def test_export_users_neutralizes_csv_formula_injection(app, session):
    """A username/full_name starting with =/+/-/@ must not export as a live formula.

    These fields are JIT-provisioned from the external issuer's userinfo
    response, so an attacker who controls their own IdP profile can set them
    to a spreadsheet-formula payload; opening the exported CSV in
    Excel/Sheets/LibreOffice must not execute it.
    """
    user = User(
        email="attacker@example.org",
        username='=HYPERLINK("http://evil","x")',
        full_name="+cmd|'/c calc'!A1",
    )
    session.add(user)
    session.commit()

    runner = CliRunner()
    result = runner.invoke(reana_admin, ["user-export"])

    rows = list(csv.reader(io.StringIO(result.output)))
    (exported,) = [row for row in rows if row[0] == str(user.id_)]
    assert exported[2] == '\'=HYPERLINK("http://evil","x")'
    assert exported[3] == "'+cmd|'/c calc'!A1"
    assert not exported[2].startswith(("=", "+", "-", "@"))
    assert not exported[3].startswith(("=", "+", "-", "@"))


def test_import_users(app, session, user0):
    """Test importing users from CSV file."""
    runner = CliRunner()
    expected_output = "Users successfully imported."
    users_csv_file_name = "reana-users.csv"
    user_id = uuid.uuid4()
    user_email = "test@reana.io"
    user_username = "jdoe"
    user_full_name = "John Doe"
    with runner.isolated_filesystem():
        with open(users_csv_file_name, "w") as f:
            csv_writer = csv.writer(f, dialect="unix")
            csv_writer.writerow([user_id, user_email, user_username, user_full_name])

        result = runner.invoke(
            reana_admin,
            [
                "user-import",
                "--file",
                users_csv_file_name,
            ],
        )
        assert expected_output in result.output
        user = session.query(User).filter_by(id_=user_id).first()
        assert user
        assert user.email == user_email
        assert user.username == user_username
        assert user.full_name == user_full_name


def test_import_users_accepts_legacy_token_column(app, session):
    """Legacy five-column exports import without restoring access tokens."""
    runner = CliRunner()
    user_id = uuid.uuid4()
    with runner.isolated_filesystem():
        with open("legacy-users.csv", "w") as csv_file:
            csv.writer(csv_file, dialect="unix").writerow(
                [
                    user_id,
                    "legacy@reana.io",
                    "legacy-secret-token",
                    "legacy-user",
                    "Legacy User",
                ]
            )

        result = runner.invoke(
            reana_admin, ["user-import", "--file", "legacy-users.csv"]
        )

    assert result.exit_code == 0, result.output
    user = session.query(User).filter_by(id_=user_id).one()
    assert user.username == "legacy-user"
    assert user.full_name == "Legacy User"


def test_create_admin_user_with_explicit_identity(app, session):
    """Admin bootstrap can create a row already linked to its OIDC identity."""
    user_id = uuid.uuid4()
    with patch("reana_server.reana_admin.cli.create_user_workspace"):
        result = CliRunner().invoke(
            reana_admin,
            [
                "create-admin-user",
                "--id",
                str(user_id),
                "--email",
                "admin-link@example.org",
                "--idp-issuer",
                "https://auth.example.org/realms/reana",
                "--idp-subject",
                "keycloak-admin-id",
            ],
        )

    assert result.exit_code == 0, result.output
    user = session.query(User).filter_by(id_=user_id).one()
    assert user.idp_issuer == "https://auth.example.org/realms/reana"
    assert user.idp_subject == "keycloak-admin-id"


def test_create_admin_user_can_link_existing_unlinked_row(app, session):
    """Rerunning bootstrap explicitly links an existing unlinked admin row."""
    user_id = uuid.uuid4()
    user = User(id_=user_id, email="existing-admin@example.org")
    session.add(user)
    session.commit()

    result = CliRunner().invoke(
        reana_admin,
        [
            "create-admin-user",
            "--id",
            str(user_id),
            "--email",
            user.email,
            "--idp-issuer",
            "https://auth.example.org/realms/reana",
            "--idp-subject",
            "existing-admin-id",
        ],
    )

    assert result.exit_code == 0
    session.refresh(user)
    assert user.idp_subject == "existing-admin-id"


def test_create_admin_user_requires_complete_identity_pair(app):
    """Partial identity input fails instead of creating an ambiguous link."""
    result = CliRunner().invoke(
        reana_admin,
        [
            "create-admin-user",
            "--id",
            str(uuid.uuid4()),
            "--email",
            "partial-admin@example.org",
            "--idp-issuer",
            "https://auth.example.org/realms/reana",
        ],
    )

    assert result.exit_code == 1
    assert "must be provided together" in result.output


def test_link_user_identity(app, session):
    """An existing user can be explicitly linked by an administrator."""
    user = User(email="migration-user@example.org")
    session.add(user)
    session.commit()

    result = CliRunner().invoke(
        reana_admin,
        [
            "link-user-identity",
            "--email",
            user.email,
            "--idp-issuer",
            "https://auth.example.org/realms/reana",
            "--idp-subject",
            "migration-subject",
        ],
    )

    assert result.exit_code == 0
    session.refresh(user)
    assert user.idp_subject == "migration-subject"


def test_link_user_identity_dry_run(app, session):
    """Dry-run validates but does not persist the identity link."""
    user = User(email="dry-run-user@example.org")
    session.add(user)
    session.commit()

    result = CliRunner().invoke(
        reana_admin,
        [
            "link-user-identity",
            "--email",
            user.email,
            "--idp-issuer",
            "https://auth.example.org/realms/reana",
            "--idp-subject",
            "dry-run-subject",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    session.refresh(user)
    assert user.idp_subject is None


def test_link_user_identity_rejects_identity_conflict(app, session):
    """An identity already owned by another user cannot be reassigned."""
    issuer = "https://auth.example.org/realms/reana"
    owner = User(
        email="identity-owner@example.org",
        idp_issuer=issuer,
        idp_subject="owned-subject",
    )
    target = User(email="identity-target@example.org")
    session.add_all([owner, target])
    session.commit()

    result = CliRunner().invoke(
        reana_admin,
        [
            "link-user-identity",
            "--email",
            target.email,
            "--idp-issuer",
            issuer,
            "--idp-subject",
            "owned-subject",
        ],
    )

    assert result.exit_code == 1
    assert "already linked to another user" in result.output
    session.refresh(target)
    assert target.idp_subject is None


def test_gitlab_webhook_revoke_deauthorizes_but_keeps_secret(app, session):
    """Revocation takes effect immediately without rotating the secret."""
    user = User(
        email="revoke-me@example.org",
        gitlab_webhook_secret="installed-in-gitlab",
        gitlab_webhook_secret_expires_at=naive_utcnow() + datetime.timedelta(days=30),
    )
    session.add(user)
    session.commit()

    result = CliRunner().invoke(
        reana_admin, ["gitlab-webhook-revoke", "--email", user.email]
    )

    assert result.exit_code == 0
    session.refresh(user)
    assert user.gitlab_webhook_secret_expires_at is None
    assert user.gitlab_webhook_secret == "installed-in-gitlab"
    with app.test_request_context(
        headers={"X-Gitlab-Token": "installed-in-gitlab"}
    ), pytest.raises(InvalidTokenError):
        _get_user_from_gitlab_secret("installed-in-gitlab")


def test_gitlab_webhook_revoke_can_delete_the_secret(app, session):
    """A compromised secret can be removed outright."""
    user = User(
        email="compromised@example.org",
        gitlab_webhook_secret="leaked-secret",
        gitlab_webhook_secret_expires_at=naive_utcnow() + datetime.timedelta(days=30),
    )
    session.add(user)
    session.commit()

    result = CliRunner().invoke(
        reana_admin,
        ["gitlab-webhook-revoke", "--email", user.email, "--delete-secret"],
    )

    assert result.exit_code == 0
    session.refresh(user)
    assert user.gitlab_webhook_secret is None
    assert user.gitlab_webhook_secret_expires_at is None


def test_gitlab_webhook_revoke_dry_run_changes_nothing(app, session):
    """Dry-run reports the revocation without persisting it."""
    expires_at = naive_utcnow() + datetime.timedelta(days=30)
    user = User(
        email="dry-run-revoke@example.org",
        gitlab_webhook_secret="still-valid",
        gitlab_webhook_secret_expires_at=expires_at,
    )
    session.add(user)
    session.commit()

    result = CliRunner().invoke(
        reana_admin, ["gitlab-webhook-revoke", "--email", user.email, "--dry-run"]
    )

    assert result.exit_code == 0
    assert "Would revoke" in result.output
    session.refresh(user)
    assert user.gitlab_webhook_secret == "still-valid"
    assert user.gitlab_webhook_secret_expires_at == expires_at


def test_gitlab_webhook_revoke_requires_a_user(app, session):
    """The command refuses to run without an explicit user selection."""
    result = CliRunner().invoke(reana_admin, ["gitlab-webhook-revoke"])

    assert result.exit_code == 1
    assert "--email or --id" in result.output


def test_gitlab_webhook_revoke_without_configured_secret(app, session):
    """Revoking a user who never enabled GitLab succeeds and says so."""
    user = User(email="no-webhook@example.org")
    session.add(user)
    session.commit()

    result = CliRunner().invoke(
        reana_admin, ["gitlab-webhook-revoke", "--email", user.email]
    )

    assert result.exit_code == 0
    assert "no GitLab webhook authorization" in result.output


def test_status_report_can_send_email():
    """Test that status reports still use the shared SMTP email helper."""

    class Status:
        def get_status(self):
            return {"ok": True}

    runner = CliRunner()
    with patch(
        "reana_server.reana_admin.cli.STATUS_OBJECT_TYPES", {"status": Status}
    ), patch("reana_server.reana_admin.cli.send_email") as send_email:
        result = runner.invoke(
            reana_admin,
            ["status-report", "--email", "admin@example.org"],
        )

    assert result.exit_code == 0
    send_email.assert_called_once()
    assert send_email.call_args.args[0] == "admin@example.org"


class TestMessageConsumer:
    def test_do_not_remove_message(
        self,
        in_memory_queue_connection,
        default_in_memory_producer,
        consume_queue,
    ):
        """Test if MessageConsumer ignores and re-queues not matching message."""
        workflow_name = "workflow.1"
        queue_name = "workflow-submission"
        consumer = MessageConsumer(
            connection=in_memory_queue_connection,
            queue_name=queue_name,
            key="workflow_id_or_name",
            values_to_delete=["some_other_name"],
        )
        in_memory_wsp = WorkflowSubmissionPublisher(
            connection=in_memory_queue_connection
        )

        in_memory_wsp.publish_workflow_submission("1", workflow_name, {})
        consume_queue(consumer, limit=1)
        assert not in_memory_queue_connection.channel().queues[queue_name].empty()
        in_memory_queue_connection.channel().queues.clear()

    def test_removes_message(
        self,
        in_memory_queue_connection,
        default_in_memory_producer,
        consume_queue,
    ):
        """Test if MessageConsumer correctly removes specified message."""
        workflow_name = "workflow.1"
        consumer = MessageConsumer(
            connection=in_memory_queue_connection,
            queue_name="workflow-submission",
            key="workflow_id_or_name",
            values_to_delete=[workflow_name],
        )
        in_memory_wsp = WorkflowSubmissionPublisher(
            connection=in_memory_queue_connection
        )

        in_memory_wsp.publish_workflow_submission("1", workflow_name, {})
        consume_queue(consumer, limit=1)
        assert (
            in_memory_queue_connection.channel().queues["workflow-submission"].empty()
        )
        in_memory_queue_connection.channel().queues.clear()


@pytest.mark.parametrize(
    "file_or_dir, expected_result",
    [
        ("in.txt", True),
        ("in", True),
        ("in/xyz.txt", True),
        ("in/subdir/xyz.txt", True),
        ("out.txt", True),
        ("out", True),
        ("out/xyz.txt", True),
        ("out/subdir/xyz.txt", True),
        ("xyz/in.txt", False),
        ("xyz/out.txt", False),
        ("abc.xyz", False),
    ],
)
def test_is_input_or_output(file_or_dir, expected_result):
    """Test if inputs/outputs are correctly recognized."""
    workspace = pathlib.Path("/workspace")

    rule = Mock()
    rule.id_ = "1234"
    rule.workflow.id_ = "5678"
    rule.workflow.reana_specification = {
        "inputs": {
            "files": ["in.txt"],
            "directories": ["in"],
        },
        "outputs": {
            "files": ["out.txt"],
            "directories": ["out"],
        },
    }
    rule.workflow.workspace_path = str(workspace)
    rule.workspace_files = "**/*"

    assert RetentionRuleDeleter(rule).is_input_output(file_or_dir) == expected_result


@pytest.mark.parametrize(
    "time_delta, to_be_kept, to_be_deleted",
    [
        (
            None,
            [
                "input.txt",
                "inputs/input.txt",
                "output.txt",
                "outputs/output.txt",
                "to_be_deleted/input.txt",
                "to_be_deleted/outputs/output.txt",
                "not_deleted.xyz",
            ],
            [
                "to_be_deleted/deleted.xyz",
                "deleted.txt",
            ],
        ),
        (
            datetime.timedelta(days=-2),
            ["input.txt", "to_be_deleted/xyz.txt"],
            [],
        ),
        (
            datetime.timedelta(days=+2),
            ["input.txt", "to_be_deleted/outputs/123.txt"],
            ["to_be_deleted/xyz.txt", "xyz.zip", "xyz.txt"],
        ),
    ],
)
def test_retention_rules_apply(
    user0,
    workflow_with_retention_rules,
    session,
    time_delta,
    to_be_kept,
    to_be_deleted,
):
    """Test the deletion of files when applying retention rules."""

    def invoke(flags):
        runner = CliRunner()
        result = runner.invoke(reana_admin, flags)
        assert result.exit_code == 0

    def init_workspace(workspace, files):
        for file in files:
            f = workspace / file
            f.parent.mkdir(parents=True, exist_ok=True)
            f.touch()
            assert f.exists()

    workflow = workflow_with_retention_rules
    workspace = pathlib.Path(workflow.workspace_path)

    other_user = User(email="xyz@cern.ch")
    session.add(other_user)
    other_workflow = Workflow(
        id_=uuid.uuid4(),
        name="other_workflow",
        owner_id=other_user.id_,
        reana_specification={},
        type_="serial",
    )
    session.add(other_workflow)
    session.commit()

    command = [
        "retention-rules-apply",
    ]
    if time_delta is not None:
        forced_date = datetime.datetime.now() + time_delta
        command += ["--force-date", forced_date.strftime("%Y-%m-%dT%H:%M:%S")]

    init_workspace(workspace, to_be_kept + to_be_deleted)

    # these invocations should not delete any file
    for other_flags in [
        ["--dry-run"],
        ["--dry-run", "--email", workflow.owner.email],
        ["--dry-run", "--id", workflow.owner.id_],
        ["--dry-run", "--workflow", workflow.id_],
        ["--email", other_user.email],
        ["--id", other_user.id_],
        ["--workflow", other_workflow.id_],
    ]:
        with patch("click.confirm"):
            invoke(command + other_flags)
    for file in to_be_deleted:
        assert workspace.joinpath(file).exists()

    with patch("click.confirm"):
        init_workspace(workspace, to_be_kept + to_be_deleted)
        invoke(command)

    for file in to_be_kept:
        assert workspace.joinpath(file).exists()
    for file in to_be_deleted:
        assert not workspace.joinpath(file).exists()


@patch("reana_server.reana_admin.cli.RetentionRuleDeleter.apply_rule")
def test_retention_rules_apply_error(
    apply_rule_mock: Mock, workflow_with_retention_rules, user0
):
    """Test that rules are reset to `active` if there are errors."""
    workflow = workflow_with_retention_rules
    apply_rule_mock.side_effect = Exception()

    runner = CliRunner()
    result = runner.invoke(
        reana_admin,
        [
            "retention-rules-apply",
        ],
    )

    assert result.exit_code == 0
    assert "Error while applying rule" in result.output
    apply_rule_mock.assert_called()
    for rule in workflow.retention_rules:
        assert rule.status == WorkspaceRetentionRuleStatus.active


def test_retention_rules_contention_preserves_rule_states(
    workflow_with_retention_rules, user0
):
    """A busy workspace is retried without active/pending state changes."""
    workflow = workflow_with_retention_rules
    original_states = {rule.id_: rule.status for rule in workflow.retention_rules}

    with patch(
        "reana_server.reana_admin.cli.workspace_mutation_lock",
        side_effect=WorkspaceMutationConflict(),
    ):
        result = CliRunner().invoke(
            reana_admin,
            [
                "retention-rules-apply",
            ],
        )

    assert result.exit_code == 0
    assert "will be retried later" in result.output
    assert {
        rule.id_: rule.status for rule in workflow.retention_rules
    } == original_states


def test_retention_rules_extend(workflow_with_retention_rules, user0):
    """Test extending of retention rules."""
    workflow = workflow_with_retention_rules
    runner = CliRunner()
    extend_days = 5

    result = runner.invoke(
        reana_admin,
        [
            "retention-rules-extend",
            "-w non-valid-id",
            "-d",
            extend_days,
        ],
    )
    assert result.output == "Invalid workflow UUID.\n"
    assert result.exit_code == 1

    result = runner.invoke(
        reana_admin,
        [
            "retention-rules-extend",
            "-w",
            workflow.id_,
            "-d",
            extend_days,
        ],
    )
    assert "Extending rule" in result.output
    assert result.exit_code == 0

    for rule in workflow.retention_rules:
        if rule.status == WorkspaceRetentionRuleStatus.active:
            assert rule.retention_days > extend_days


def test_retention_rule_deleter_file_outside_workspace(tmp_path):
    """Test that file outside the workspace are not deleted."""
    file = tmp_path.joinpath("do_not_delete.txt")
    file.write_text("Must be preserved")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    rule = Mock()
    rule.id_ = "1234"
    rule.workflow.id_ = "5678"
    rule.workflow.reana_specification = {}
    rule.workflow.workspace_path = str(workspace)
    rule.workspace_files = "../**/*.txt"

    RetentionRuleDeleter(rule).apply_rule()

    assert file.exists()


@pytest.mark.parametrize(
    "days, output", [(0, "has been closed"), (5, "Leaving opened")]
)
@patch("reana_server.reana_admin.cli.requests.get")
def test_interactive_session_cleanup(
    mock_requests, sample_serial_workflow_in_db, days, output, user0, session
):
    """Test closure of long running interactive sessions."""
    runner = CliRunner()

    mock_session_pod = MagicMock()
    mock_session_pod.metadata.name = f"run-session-{sample_serial_workflow_in_db.id_}-a"
    session_secret = "per-session-secret"
    interactive_session = InteractiveSession(
        name=f"run-session-{sample_serial_workflow_in_db.id_}",
        path=f"/{sample_serial_workflow_in_db.id_}",
        owner_id=sample_serial_workflow_in_db.owner_id,
        session_secret=session_secret,
    )
    sample_serial_workflow_in_db.sessions.append(interactive_session)
    session.add(sample_serial_workflow_in_db)
    session.commit()
    mock_session_pod.spec.containers[0].args = []
    mock_session_pod.metadata.labels = {
        "app": mock_session_pod.metadata.name,
        "reana_workflow_mode": "session",
        "reana-run-session-workflow-uuid": str(sample_serial_workflow_in_db.id_),
        "user-uuid": str(sample_serial_workflow_in_db.owner_id),
    }
    mock_pod_list = Mock()
    mock_pod_list.items = [mock_session_pod]
    mock_k8s_api_client = Mock()
    mock_k8s_api_client.list_namespaced_pod.return_value = mock_pod_list

    mock_requests.return_value = Mock(
        status_code=200,
        json=lambda: {
            "last_activity": datetime.date.today().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        },
    )

    with patch(
        "reana_server.reana_admin.cli.current_k8s_corev1_api_client",
        mock_k8s_api_client,
    ):
        with patch(
            "reana_server.reana_admin.cli.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(
                mock_http_response=Mock()
            ),
        ):
            result = runner.invoke(
                reana_admin,
                [
                    "interactive-session-cleanup",
                    "-d",
                    days,
                ],
            )
            assert output in result.output
            mock_requests.assert_called_once_with(
                ANY,
                headers={"Authorization": f"token {session_secret}"},
                timeout=10,
            )


@patch("reana_server.reana_admin.cli.requests.get")
def test_interactive_session_cleanup_by_user_closes_immediately(
    mock_requests, sample_serial_workflow_in_db, user0, session
):
    """--email closes a user's sessions immediately, ignoring inactivity."""
    runner = CliRunner()

    mock_session_pod = MagicMock()
    mock_session_pod.metadata.name = f"run-session-{sample_serial_workflow_in_db.id_}-a"
    session_secret = "per-session-secret"
    interactive_session = InteractiveSession(
        name=f"run-session-{sample_serial_workflow_in_db.id_}",
        path=f"/{sample_serial_workflow_in_db.id_}",
        owner_id=sample_serial_workflow_in_db.owner_id,
        session_secret=session_secret,
    )
    sample_serial_workflow_in_db.sessions.append(interactive_session)
    session.add(sample_serial_workflow_in_db)
    session.commit()
    mock_session_pod.spec.containers[0].args = []
    mock_session_pod.metadata.labels = {
        "app": mock_session_pod.metadata.name,
        "reana_workflow_mode": "session",
        "reana-run-session-workflow-uuid": str(sample_serial_workflow_in_db.id_),
        "user-uuid": str(sample_serial_workflow_in_db.owner_id),
    }
    mock_pod_list = Mock()
    mock_pod_list.items = [mock_session_pod]
    mock_k8s_api_client = Mock()
    mock_k8s_api_client.list_namespaced_pod.return_value = mock_pod_list

    with patch(
        "reana_server.reana_admin.cli.current_k8s_corev1_api_client",
        mock_k8s_api_client,
    ):
        with patch(
            "reana_server.reana_admin.cli.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(
                mock_http_response=Mock()
            ),
        ):
            result = runner.invoke(
                reana_admin,
                ["interactive-session-cleanup", "--email", user0.email],
            )
            assert "has been closed" in result.output
            # Immediate revocation must not depend on the session's own
            # (self-reported) activity status.
            mock_requests.assert_not_called()
            mock_k8s_api_client.list_namespaced_pod.assert_called_once()
            _, kwargs = mock_k8s_api_client.list_namespaced_pod.call_args
            assert f"user-uuid={user0.id_}" in kwargs["label_selector"]


@patch("reana_server.reana_admin.cli.requests.get")
def test_interactive_session_cleanup_by_user_closes_pre_upgrade_session(
    mock_requests, sample_serial_workflow_in_db, user0, session
):
    """--email/--id must close sessions created before session tokens existed.

    Interactive sessions created before the session-token feature shipped
    have ``session_secret is None``. Immediate revocation only needs the
    workflow/user identity to call ``close_interactive_session``, so it must
    not skip these sessions just because they have no token.
    """
    runner = CliRunner()

    mock_session_pod = MagicMock()
    mock_session_pod.metadata.name = f"run-session-{sample_serial_workflow_in_db.id_}-a"
    interactive_session = InteractiveSession(
        name=f"run-session-{sample_serial_workflow_in_db.id_}",
        path=f"/{sample_serial_workflow_in_db.id_}",
        owner_id=sample_serial_workflow_in_db.owner_id,
        session_secret=None,
    )
    sample_serial_workflow_in_db.sessions.append(interactive_session)
    session.add(sample_serial_workflow_in_db)
    session.commit()
    mock_session_pod.spec.containers[0].args = []
    mock_session_pod.metadata.labels = {
        "app": mock_session_pod.metadata.name,
        "reana_workflow_mode": "session",
        "reana-run-session-workflow-uuid": str(sample_serial_workflow_in_db.id_),
        "user-uuid": str(sample_serial_workflow_in_db.owner_id),
    }
    mock_pod_list = Mock()
    mock_pod_list.items = [mock_session_pod]
    mock_k8s_api_client = Mock()
    mock_k8s_api_client.list_namespaced_pod.return_value = mock_pod_list

    with patch(
        "reana_server.reana_admin.cli.current_k8s_corev1_api_client",
        mock_k8s_api_client,
    ):
        with patch(
            "reana_server.reana_admin.cli.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(
                mock_http_response=Mock()
            ),
        ):
            result = runner.invoke(
                reana_admin,
                ["interactive-session-cleanup", "--email", user0.email],
            )
            assert "has been closed" in result.output
            # A missing session_secret must not stop immediate revocation.
            mock_requests.assert_not_called()


def test_interactive_session_cleanup_requires_days_or_user():
    """Neither --days nor --email/--id given must fail clearly, not silently."""
    runner = CliRunner()
    result = runner.invoke(reana_admin, ["interactive-session-cleanup"])
    assert result.exit_code != 0
    assert "--days" in result.output


@patch("reana_server.reana_admin.cli.requests.get")
def test_revoke_identity_closes_everything(
    mock_requests, sample_serial_workflow_in_db, user0, session, redis_store
):
    """One command closes the session, revokes the webhook, kills the BFF session."""
    runner = CliRunner()
    user0.idp_issuer = "https://issuer.example.org"
    user0.idp_subject = "user0-subject"
    user0.gitlab_webhook_secret = "installed-in-gitlab"
    user0.gitlab_webhook_secret_expires_at = naive_utcnow() + datetime.timedelta(
        days=30
    )
    session.add(user0)
    session.commit()
    sessions_module.store_session(
        "user0-browser-session",
        "refresh",
        "id",
        "access",
        issuer=user0.idp_issuer,
        subject=user0.idp_subject,
        client_id="reana-server",
        created_at=datetime.datetime.now().timestamp(),
    )

    mock_session_pod = MagicMock()
    mock_session_pod.metadata.name = f"run-session-{sample_serial_workflow_in_db.id_}-a"
    interactive_session = InteractiveSession(
        name=f"run-session-{sample_serial_workflow_in_db.id_}",
        path=f"/{sample_serial_workflow_in_db.id_}",
        owner_id=sample_serial_workflow_in_db.owner_id,
        session_secret="per-session-secret",
    )
    sample_serial_workflow_in_db.sessions.append(interactive_session)
    session.add(sample_serial_workflow_in_db)
    session.commit()
    mock_session_pod.spec.containers[0].args = []
    mock_session_pod.metadata.labels = {
        "app": mock_session_pod.metadata.name,
        "reana_workflow_mode": "session",
        "reana-run-session-workflow-uuid": str(sample_serial_workflow_in_db.id_),
        "user-uuid": str(sample_serial_workflow_in_db.owner_id),
    }
    mock_pod_list = Mock()
    mock_pod_list.items = [mock_session_pod]
    mock_k8s_api_client = Mock()
    mock_k8s_api_client.list_namespaced_pod.return_value = mock_pod_list

    with patch(
        "reana_server.reana_admin.cli.current_k8s_corev1_api_client",
        mock_k8s_api_client,
    ), patch(
        "reana_server.reana_admin.cli.current_rwc_api_client",
        make_mock_api_client("reana-workflow-controller")(mock_http_response=Mock()),
    ):
        result = runner.invoke(reana_admin, ["revoke-identity", "--email", user0.email])

    assert result.exit_code == 0, result.output
    assert "has been closed" in result.output
    assert "Revoked the GitLab webhook authorization" in result.output
    assert "Deleted 1 browser session(s)" in result.output
    assert "Remember to remove this user's identity-provider role" in result.output
    # revoke-identity's composed gitlab_webhook_revoke call commits, which
    # (via the Flask app-context teardown at the end of CliRunner.invoke)
    # expunges every object the shared scoped_session was tracking --
    # including sample_serial_workflow_in_db's own object, whose fixture
    # teardown later lazily loads the workflow's related rows. Re-attach and
    # load them here so teardown does not itself fail with
    # DetachedInstanceError. This is a test-fixture interaction, not something
    # revoke-identity itself needs to account for in production.
    session.add(sample_serial_workflow_in_db)
    list(sample_serial_workflow_in_db.jobs)
    list(sample_serial_workflow_in_db.resources)
    session.refresh(user0)
    assert user0.gitlab_webhook_secret_expires_at is None
    assert user0.gitlab_webhook_secret == "installed-in-gitlab"
    assert sessions_module.get_session("user0-browser-session") is None


def test_revoke_identity_dry_run_changes_nothing(user0, session, redis_store):
    """Dry-run reports what would happen across all three subsystems, changes none."""
    runner = CliRunner()
    user0.idp_issuer = "https://issuer.example.org"
    user0.idp_subject = "user0-subject"
    user0.gitlab_webhook_secret = "still-valid"
    user0.gitlab_webhook_secret_expires_at = naive_utcnow() + datetime.timedelta(
        days=30
    )
    session.add(user0)
    session.commit()
    sessions_module.store_session(
        "user0-browser-session",
        "refresh",
        "id",
        "access",
        issuer=user0.idp_issuer,
        subject=user0.idp_subject,
        client_id="reana-server",
        created_at=datetime.datetime.now().timestamp(),
    )
    mock_k8s_api_client = Mock()
    mock_k8s_api_client.list_namespaced_pod.return_value = Mock(items=[])

    with patch(
        "reana_server.reana_admin.cli.current_k8s_corev1_api_client",
        mock_k8s_api_client,
    ):
        result = runner.invoke(
            reana_admin, ["revoke-identity", "--email", user0.email, "--dry-run"]
        )

    assert result.exit_code == 0, result.output
    assert "Would revoke" in result.output
    assert "Would delete 1 browser session(s)" in result.output
    session.refresh(user0)
    assert user0.gitlab_webhook_secret == "still-valid"
    assert user0.gitlab_webhook_secret_expires_at is not None
    assert sessions_module.get_session("user0-browser-session") is not None


def test_revoke_identity_delete_secret_passthrough(user0, session):
    """--delete-secret forwards through to the composed webhook revocation."""
    runner = CliRunner()
    user0.gitlab_webhook_secret = "leaked-secret"
    user0.gitlab_webhook_secret_expires_at = naive_utcnow() + datetime.timedelta(
        days=30
    )
    session.add(user0)
    session.commit()
    mock_k8s_api_client = Mock()
    mock_k8s_api_client.list_namespaced_pod.return_value = Mock(items=[])

    with patch(
        "reana_server.reana_admin.cli.current_k8s_corev1_api_client",
        mock_k8s_api_client,
    ):
        result = runner.invoke(
            reana_admin,
            ["revoke-identity", "--email", user0.email, "--delete-secret"],
        )

    assert result.exit_code == 0, result.output
    session.refresh(user0)
    assert user0.gitlab_webhook_secret is None
    assert user0.gitlab_webhook_secret_expires_at is None


def test_revoke_identity_requires_a_user(app):
    """The command refuses to run without an explicit user selection."""
    result = CliRunner().invoke(reana_admin, ["revoke-identity"])

    assert result.exit_code == 1
    assert "--email or --id" in result.output


def test_revoke_identity_without_linked_identity(user0, session):
    """A never-logged-in account has no BFF sessions to look up -- not an error."""
    runner = CliRunner()
    assert user0.idp_subject is None
    mock_k8s_api_client = Mock()
    mock_k8s_api_client.list_namespaced_pod.return_value = Mock(items=[])

    with patch(
        "reana_server.reana_admin.cli.current_k8s_corev1_api_client",
        mock_k8s_api_client,
    ):
        result = runner.invoke(reana_admin, ["revoke-identity", "--email", user0.email])

    assert result.exit_code == 0, result.output
    assert "no linked identity-provider subject" in result.output


class TestCheckWorkflows:
    @patch(
        "reana_server.reana_admin.check_workflows._collect_messages_from_scheduler_queue",
        Mock(return_value={}),
    )
    def test_check_correct_queued_workflow(self, session, sample_serial_workflow_in_db):
        sample_serial_workflow_in_db.created = (
            datetime.datetime.now() - datetime.timedelta(hours=12)
        )
        sample_serial_workflow_in_db.status = RunStatus.queued
        session.add(sample_serial_workflow_in_db)
        session.commit()

        mock_messages = {
            str(sample_serial_workflow_in_db.id_): {"some_key": "some_value"},
        }

        with patch(
            "reana_server.reana_admin.check_workflows._get_all_pods",
            Mock(return_value=[]),
        ):
            with patch(
                "reana_server.reana_admin.check_workflows._collect_messages_from_scheduler_queue",
                Mock(return_value=mock_messages),
            ):
                from reana_server.reana_admin.check_workflows import check_workflows

                in_sync, out_of_sync, total_workflows = check_workflows(
                    datetime.datetime.now() - datetime.timedelta(hours=24), None
                )
                assert total_workflows == 1
                assert len(out_of_sync) == 0
                assert len(in_sync) == 1

    @patch(
        "reana_server.reana_admin.check_workflows._collect_messages_from_scheduler_queue",
        Mock(return_value={}),
    )
    def test_check_correct_pending_workflow(
        self, session, sample_serial_workflow_in_db
    ):
        sample_serial_workflow_in_db.created = (
            datetime.datetime.now() - datetime.timedelta(hours=12)
        )
        sample_serial_workflow_in_db.status = RunStatus.pending
        session.add(sample_serial_workflow_in_db)
        session.commit()

        mock_pod = Mock()
        mock_pod.metadata.name = f"run-batch-{sample_serial_workflow_in_db.id_}"
        mock_pod.status.phase = "Pending"

        with patch(
            "reana_server.reana_admin.check_workflows._get_all_pods",
            Mock(return_value=[mock_pod]),
        ):
            from reana_server.reana_admin.check_workflows import check_workflows

            in_sync, out_of_sync, total_workflows = check_workflows(
                datetime.datetime.now() - datetime.timedelta(hours=24), None
            )
            assert total_workflows == 1
            assert len(out_of_sync) == 0
            assert len(in_sync) == 1

    @patch(
        "reana_server.reana_admin.check_workflows._collect_messages_from_scheduler_queue",
        Mock(return_value={}),
    )
    def test_check_correct_running_workflow(
        self, session, sample_serial_workflow_in_db
    ):
        sample_serial_workflow_in_db.created = (
            datetime.datetime.now() - datetime.timedelta(hours=12)
        )
        sample_serial_workflow_in_db.status = RunStatus.running
        session.add(sample_serial_workflow_in_db)
        session.commit()

        mock_pod = Mock()
        mock_pod.metadata.name = f"run-batch-{sample_serial_workflow_in_db.id_}"
        mock_pod.status.phase = "Running"
        mock_container = Mock()
        mock_container.state.terminated = {}
        mock_pod.status.container_statuses = [mock_container]

        with patch(
            "reana_server.reana_admin.check_workflows._get_all_pods",
            Mock(return_value=[mock_pod]),
        ):
            from reana_server.reana_admin.check_workflows import check_workflows

            in_sync, out_of_sync, total_workflows = check_workflows(
                datetime.datetime.now() - datetime.timedelta(hours=24), None
            )
            assert total_workflows == 1
            assert len(out_of_sync) == 0
            assert len(in_sync) == 1

    @patch(
        "reana_server.reana_admin.check_workflows._collect_messages_from_scheduler_queue",
        Mock(return_value={}),
    )
    def test_check_correct_finished_workflow(
        self, session, sample_serial_workflow_in_db
    ):
        sample_serial_workflow_in_db.created = (
            datetime.datetime.now() - datetime.timedelta(hours=12)
        )
        sample_serial_workflow_in_db.status = RunStatus.finished
        session.add(sample_serial_workflow_in_db)
        session.commit()

        with patch(
            "reana_server.reana_admin.check_workflows._get_all_pods",
            Mock(return_value=[]),
        ):
            from reana_server.reana_admin.check_workflows import check_workflows

            in_sync, out_of_sync, total_workflows = check_workflows(
                datetime.datetime.now() - datetime.timedelta(hours=24), None
            )
            assert total_workflows == 1
            assert len(out_of_sync) == 0
            assert len(in_sync) == 1

    @patch(
        "reana_server.reana_admin.check_workflows._collect_messages_from_scheduler_queue",
        Mock(return_value={}),
    )
    def test_check_workflow_without_workspace(
        self, session, sample_serial_workflow_in_db
    ):
        sample_serial_workflow_in_db.created = (
            datetime.datetime.now() - datetime.timedelta(hours=12)
        )
        sample_serial_workflow_in_db.status = RunStatus.finished
        # change workspace path to invalid directory
        sample_serial_workflow_in_db.workspace_path = (
            sample_serial_workflow_in_db.workspace_path + "xyz"
        )
        session.add(sample_serial_workflow_in_db)
        session.commit()

        with patch(
            "reana_server.reana_admin.check_workflows._get_all_pods",
            Mock(return_value=[]),
        ):
            from reana_server.reana_admin.check_workflows import check_workflows

            in_sync, out_of_sync, total_workflows = check_workflows(
                datetime.datetime.now() - datetime.timedelta(hours=24), None
            )
            assert total_workflows == 1
            assert len(out_of_sync) == 1
            assert len(in_sync) == 0
            assert out_of_sync[0].source.id == str(sample_serial_workflow_in_db.id_)
            assert len(out_of_sync[0].errors) == 1
            assert "not exist" in str(out_of_sync[0].errors[0])

    def test_check_correct_created_session(self, session, sample_serial_workflow_in_db):
        interactive_session = InteractiveSession(
            name=f"run-session-{sample_serial_workflow_in_db.id_}",
            path="some-path",
            owner_id=sample_serial_workflow_in_db.owner_id,
            status=RunStatus.created,
        )
        sample_serial_workflow_in_db.sessions.append(interactive_session)
        session.add(sample_serial_workflow_in_db)
        session.commit()

        mock_session_pod = Mock()
        mock_session_pod.metadata.name = (
            f"run-session-{sample_serial_workflow_in_db.id_}-a"
        )
        mock_session_pod.status.phase = "Running"

        mock_batch_pod = Mock()
        mock_batch_pod.metadata.name = f"run-batch-{sample_serial_workflow_in_db.id_}-b"

        with patch(
            "reana_server.reana_admin.check_workflows._get_all_pods",
            Mock(return_value=[mock_batch_pod, mock_session_pod]),
        ):
            from reana_server.reana_admin.check_workflows import (
                check_interactive_sessions,
            )

            (
                in_sync,
                out_of_sync,
                pods_without_session,
                total_sessions,
            ) = check_interactive_sessions()
            assert total_sessions == 1
            assert len(pods_without_session) == 0
            assert len(out_of_sync) == 0
            assert len(in_sync) == 1

    def test_check_session_has_more_than_one_pod(
        self, session, sample_serial_workflow_in_db
    ):
        interactive_session = InteractiveSession(
            name=f"run-session-{sample_serial_workflow_in_db.id_}",
            path="some-path",
            owner_id=sample_serial_workflow_in_db.owner_id,
            status=RunStatus.created,
        )
        sample_serial_workflow_in_db.sessions.append(interactive_session)
        session.add(sample_serial_workflow_in_db)
        session.commit()

        mock_session_pod = Mock()
        mock_session_pod.metadata.name = (
            f"run-session-{sample_serial_workflow_in_db.id_}-a"
        )
        mock_session_pod.status.phase = "Running"

        mock_session_pod_2 = Mock()
        mock_session_pod_2.metadata.name = (
            f"run-session-{sample_serial_workflow_in_db.id_}-b"
        )
        mock_session_pod_2.status.phase = "Running"

        with patch(
            "reana_server.reana_admin.check_workflows._get_all_pods",
            Mock(return_value=[mock_session_pod_2, mock_session_pod]),
        ):
            from reana_server.reana_admin.check_workflows import (
                check_interactive_sessions,
            )

            (
                in_sync,
                out_of_sync,
                pods_without_session,
                total_sessions,
            ) = check_interactive_sessions()
            assert total_sessions == 1
            assert len(pods_without_session) == 0
            assert len(in_sync) == 0
            assert len(out_of_sync) == 1

            assert "Only one pod should exist." in str(out_of_sync[0].errors[0])

    def test_check_session_is_missing_pod(self, session, sample_serial_workflow_in_db):
        interactive_session = InteractiveSession(
            name=f"run-session-{sample_serial_workflow_in_db.id_}",
            path="some-path",
            owner_id=sample_serial_workflow_in_db.owner_id,
            status=RunStatus.created,
        )
        sample_serial_workflow_in_db.sessions.append(interactive_session)
        session.add(sample_serial_workflow_in_db)
        session.commit()

        with patch(
            "reana_server.reana_admin.check_workflows._get_all_pods",
            Mock(return_value=[]),
        ):
            from reana_server.reana_admin.check_workflows import (
                check_interactive_sessions,
            )

            (
                in_sync,
                out_of_sync,
                pods_without_session,
                total_sessions,
            ) = check_interactive_sessions()
            assert total_sessions == 1
            assert len(pods_without_session) == 0
            assert len(in_sync) == 0
            assert len(out_of_sync) == 1

    def test_check_pod_is_missing_session(self, session, sample_serial_workflow_in_db):
        interactive_session = InteractiveSession(
            name=f"run-session-{sample_serial_workflow_in_db.id_}",
            path="some-path",
            owner_id=sample_serial_workflow_in_db.owner_id,
            status=RunStatus.created,
        )
        sample_serial_workflow_in_db.sessions.append(interactive_session)
        session.add(sample_serial_workflow_in_db)
        session.commit()

        mock_session_pod = Mock()
        mock_session_pod.metadata.name = (
            f"run-session-{sample_serial_workflow_in_db.id_}-a"
        )
        mock_session_pod.status.phase = "Running"

        session.delete(interactive_session)
        session.commit()

        with patch(
            "reana_server.reana_admin.check_workflows._get_all_pods",
            Mock(return_value=[mock_session_pod]),
        ):
            from reana_server.reana_admin.check_workflows import (
                check_interactive_sessions,
            )

            (
                in_sync,
                out_of_sync,
                pods_without_session,
                total_sessions,
            ) = check_interactive_sessions()
            assert total_sessions == 0
            assert len(in_sync) == 0
            assert len(out_of_sync) == 0
            assert len(pods_without_session) == 1

    @pytest.mark.parametrize(
        "user_id, workflow_id",
        [
            (None, None),  # actual user/workflow UUID saved in database
            (generate_uuid(), generate_uuid()),
            ("random-user", "random-workflow"),
        ],
    )
    def test_check_workspaces(
        self,
        app,
        user_id,
        workflow_id,
        sample_serial_workflow_in_db,
        tmp_path: pathlib.Path,
    ):
        workflow = None
        if not workflow_id:
            workflow = sample_serial_workflow_in_db
            workflow_id = str(workflow.id_)
        user = None
        if not user_id:
            user = sample_serial_workflow_in_db.owner
            user_id = str(user.id_)

        # prepare wrong workspace
        extra_workspace_path = tmp_path.joinpath(
            "users", user_id, "workflows", workflow_id
        )
        extra_workspace_path.mkdir(parents=True)

        with patch(
            "reana_server.reana_admin.check_workflows.SHARED_VOLUME_PATH",
            str(tmp_path),
        ):
            extra_workspaces = check_workspaces()

        assert len(extra_workspaces) == 1
        result = extra_workspaces[0]
        assert result.source.workspace == str(extra_workspace_path)
        assert result.source.id == (str(workflow.id_) if workflow else None)
        assert result.source.user == (user.email if user else None)
        assert result.source.name == (workflow.name if workflow else None)
        assert result.errors
        assert any("not owned" in str(error) for error in result.errors)
        if workflow:
            assert len(result.errors) == 2
            assert any(workflow.workspace_path in str(error) for error in result.errors)


def test_quota_set_default_limits_for_user_with_custom_limits(user0, session):
    """Test setting default quota when there are is one user with custom quota limits."""
    runner = CliRunner()

    resources = session.query(Resource).all()

    for resource in resources:
        user_resource = (
            session.query(UserResource)
            .filter_by(user_id=user0.id_, resource_id=resource.id_)
            .first()
        )

        if user_resource:
            user_resource.quota_limit = 12345

    session.commit()

    result = runner.invoke(
        reana_admin,
        [
            "quota-set-default-limits",
        ],
    )

    assert "There are no users without quota limits." in result.output
