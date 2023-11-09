# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021, 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test command line application."""

import csv
import datetime
import io
import pathlib
import secrets
import uuid
from unittest.mock import MagicMock, Mock, patch

import pytest
from click.testing import CliRunner
from pytest_reana.test_utils import make_mock_api_client
from reana_db.models import (
    AuditLogAction,
    InteractiveSession,
    Resource,
    RunStatus,
    User,
    UserResource,
    UserTokenStatus,
    Workflow,
    WorkspaceRetentionRuleStatus,
    generate_uuid,
)
from reana_server.api_client import WorkflowSubmissionPublisher
from reana_server.reana_admin import reana_admin
from reana_server.reana_admin.check_workflows import check_workspaces
from reana_server.reana_admin.cli import RetentionRuleDeleter
from reana_server.reana_admin.consumer import MessageConsumer


def test_export_users(user0):
    """Test exporting all users as csv."""
    runner = CliRunner()
    expected_csv_file = io.StringIO()
    csv_writer = csv.writer(expected_csv_file, dialect="unix")
    csv_writer.writerow(
        [
            user0.id_,
            user0.email,
            user0.access_token,
            user0.username,
            user0.full_name,
        ]
    )
    result = runner.invoke(
        reana_admin, ["user-export", "--admin-access-token", user0.access_token]
    )
    assert result.output == expected_csv_file.getvalue()


def test_import_users(app, session, user0):
    """Test importing users from CSV file."""
    runner = CliRunner()
    expected_output = "Users successfully imported."
    users_csv_file_name = "reana-users.csv"
    user_id = uuid.uuid4()
    user_email = "test@reana.io"
    user_access_token = secrets.token_urlsafe(16)
    user_username = "jdoe"
    user_full_name = "John Doe"
    with runner.isolated_filesystem():
        with open(users_csv_file_name, "w") as f:
            csv_writer = csv.writer(f, dialect="unix")
            csv_writer.writerow(
                [user_id, user_email, user_access_token, user_username, user_full_name]
            )

        result = runner.invoke(
            reana_admin,
            [
                "user-import",
                "--admin-access-token",
                user0.access_token,
                "--file",
                users_csv_file_name,
            ],
        )
        assert expected_output in result.output
        user = session.query(User).filter_by(id_=user_id).first()
        assert user
        assert user.email == user_email
        assert user.access_token == user_access_token
        assert user.username == user_username
        assert user.full_name == user_full_name


def test_grant_token(user0, session):
    """Test grant access token."""
    runner = CliRunner()

    # non-existing email user
    result = runner.invoke(
        reana_admin,
        [
            "token-grant",
            "--admin-access-token",
            user0.access_token,
            "-e",
            "nonexisting@example.org",
        ],
    )
    assert "does not exist" in result.output

    # non-existing id user
    result = runner.invoke(
        reana_admin,
        [
            "token-grant",
            "--admin-access-token",
            user0.access_token,
            "--id",
            "fake_id",
        ],
    )
    assert "does not exist" in result.output

    # non-requested-token user
    user = User(email="johndoe@cern.ch")
    session.add(user)
    session.commit()
    result = runner.invoke(
        reana_admin,
        [
            "token-grant",
            "--admin-access-token",
            user0.access_token,
            "-e",
            user.email,
        ],
    )
    assert "token status is None, do you want to proceed?" in result.output

    # abort grant
    result = runner.invoke(
        reana_admin,
        [
            "token-grant",
            "--admin-access-token",
            user0.access_token,
            "-e",
            user.email,
        ],
        input="\n",
    )
    assert "Grant token aborted" in result.output

    # confirm grant
    result = runner.invoke(
        reana_admin,
        [
            "token-grant",
            "--admin-access-token",
            user0.access_token,
            "-e",
            user.email,
        ],
        input="y\n",
    )
    assert f"Token for user {user.id_} ({user.email}) granted" in result.output
    assert user.access_token
    assert user0.audit_logs[-1].action is AuditLogAction.grant_token

    # user with active token
    active_user = User(email="active@cern.ch", access_token="valid_token")
    session.add(active_user)
    session.commit()
    result = runner.invoke(
        reana_admin,
        [
            "token-grant",
            "--admin-access-token",
            user0.access_token,
            "--id",
            str(active_user.id_),
        ],
    )
    assert "has already an active access token" in result.output

    # typical ui user workflow
    ui_user = User(email="ui_user@cern.ch")
    session.add(ui_user)
    session.commit()
    ui_user.request_access_token()
    assert ui_user.access_token_status is UserTokenStatus.requested.name
    assert ui_user.access_token is None
    result = runner.invoke(
        reana_admin,
        [
            "token-grant",
            "--admin-access-token",
            user0.access_token,
            "--id",
            str(ui_user.id_),
        ],
    )
    assert ui_user.access_token_status is UserTokenStatus.active.name
    assert ui_user.access_token
    assert user0.audit_logs[-1].action is AuditLogAction.grant_token


def test_revoke_token(user0, session):
    """Test revoke access token."""
    runner = CliRunner()

    # non-active-token user
    user = User(email="janedoe@cern.ch")
    session.add(user)
    session.commit()
    result = runner.invoke(
        reana_admin,
        [
            "token-revoke",
            "--admin-access-token",
            user0.access_token,
            "-e",
            user.email,
        ],
    )
    assert "does not have an active access token" in result.output

    # user with requested token
    user.request_access_token()
    assert user.access_token_status == UserTokenStatus.requested.name
    result = runner.invoke(
        reana_admin,
        [
            "token-revoke",
            "--admin-access-token",
            user0.access_token,
            "-e",
            user.email,
        ],
    )
    assert "does not have an active access token" in result.output

    # user with active token
    user.access_token = "active_token"
    session.commit()
    assert user.access_token
    result = runner.invoke(
        reana_admin,
        [
            "token-revoke",
            "--admin-access-token",
            user0.access_token,
            "--id",
            str(user.id_),
        ],
    )
    assert "was successfully revoked" in result.output
    assert user.access_token_status == UserTokenStatus.revoked.name
    assert user0.audit_logs[-1].action is AuditLogAction.revoke_token

    # try to revoke again
    result = runner.invoke(
        reana_admin,
        [
            "token-revoke",
            "--admin-access-token",
            user0.access_token,
            "--id",
            str(user.id_),
        ],
    )
    assert "does not have an active access token" in result.output


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
        "--admin-access-token",
        user0.access_token,
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
            "--admin-access-token",
            user0.access_token,
        ],
    )

    assert result.exit_code == 0
    assert "Error while applying rule" in result.output
    apply_rule_mock.assert_called()
    for rule in workflow.retention_rules:
        assert rule.status == WorkspaceRetentionRuleStatus.active


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
            "--admin-access-token",
            user0.access_token,
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
            "--admin-access-token",
            user0.access_token,
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
    mock_requests, sample_serial_workflow_in_db, days, output, user0
):
    """Test closure of long running interactive sessions."""
    runner = CliRunner()

    mock_session_pod = MagicMock()
    mock_session_pod.metadata.name = f"run-session-{sample_serial_workflow_in_db.id_}-a"
    mock_session_pod.spec.containers[0].args = ["--NotebookApp.token='token'"]
    mock_session_pod.metadata.labels = {
        "app": mock_session_pod.metadata.name,
        "reana_workflow_mode": "session",
        "reana-run-session-workflow-uuid": str(sample_serial_workflow_in_db.id_),
        "reana-run-session-owner-uuid": str(sample_serial_workflow_in_db.owner_id),
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
                    "--admin-access-token",
                    user0.access_token,
                ],
            )
            assert output in result.output


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
            "--admin-access-token",
            user0.access_token,
        ],
    )

    assert "There are no users without quota limits." in result.output
