# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021, 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test command line application."""

import datetime
import csv
import io
import pathlib
import secrets
from unittest.mock import Mock, patch
import uuid

from click.testing import CliRunner
import pytest
from reana_db.models import AuditLogAction, User, UserTokenStatus, RunStatus

from reana_server.api_client import WorkflowSubmissionPublisher
from reana_server.reana_admin import reana_admin
from reana_server.reana_admin.cli import RetentionRuleDeleter
from reana_server.reana_admin.consumer import MessageConsumer


def test_export_users(default_user):
    """Test exporting all users as csv."""
    runner = CliRunner()
    expected_csv_file = io.StringIO()
    csv_writer = csv.writer(expected_csv_file, dialect="unix")
    csv_writer.writerow(
        [
            default_user.id_,
            default_user.email,
            default_user.access_token,
            default_user.username,
            default_user.full_name,
        ]
    )
    result = runner.invoke(
        reana_admin, ["user-export", "--admin-access-token", default_user.access_token]
    )
    assert result.output == expected_csv_file.getvalue()


def test_import_users(app, session, default_user):
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
                default_user.access_token,
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


def test_grant_token(default_user, session):
    """Test grant access token."""
    runner = CliRunner()

    # non-existing email user
    result = runner.invoke(
        reana_admin,
        [
            "token-grant",
            "--admin-access-token",
            default_user.access_token,
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
            default_user.access_token,
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
            default_user.access_token,
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
            default_user.access_token,
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
            default_user.access_token,
            "-e",
            user.email,
        ],
        input="y\n",
    )
    assert f"Token for user {user.id_} ({user.email}) granted" in result.output
    assert user.access_token
    assert default_user.audit_logs[-1].action is AuditLogAction.grant_token

    # user with active token
    active_user = User(email="active@cern.ch", access_token="valid_token")
    session.add(active_user)
    session.commit()
    result = runner.invoke(
        reana_admin,
        [
            "token-grant",
            "--admin-access-token",
            default_user.access_token,
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
            default_user.access_token,
            "--id",
            str(ui_user.id_),
        ],
    )
    assert ui_user.access_token_status is UserTokenStatus.active.name
    assert ui_user.access_token
    assert default_user.audit_logs[-1].action is AuditLogAction.grant_token


def test_revoke_token(default_user, session):
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
            default_user.access_token,
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
            default_user.access_token,
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
            default_user.access_token,
            "--id",
            str(user.id_),
        ],
    )
    assert "was successfully revoked" in result.output
    assert user.access_token_status == UserTokenStatus.revoked.name
    assert default_user.audit_logs[-1].action is AuditLogAction.revoke_token

    # try to revoke again
    result = runner.invoke(
        reana_admin,
        [
            "token-revoke",
            "--admin-access-token",
            default_user.access_token,
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

    assert (
        RetentionRuleDeleter(rule).is_input_output(workspace / file_or_dir)
        == expected_result
    )


def test_retention_rules_apply(workflow_with_retention_rules):
    """Test the deletion of files when applying retention rules."""
    workflow = workflow_with_retention_rules
    workspace = pathlib.Path(workflow.workspace_path)

    to_be_kept = [
        "input.txt",
        "inputs/input.txt",
        "output.txt",
        "outputs/output.txt",
        "to_be_deleted/input.txt",
        "to_be_deleted/outputs/output.txt",
        "not_deleted.xyz",
    ]
    to_be_deleted = [
        "to_be_deleted/deleted.xyz",
        "deleted.txt",
    ]

    for file in to_be_kept + to_be_deleted:
        f = workspace / file
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
        assert f.exists()

    runner = CliRunner()
    result = runner.invoke(reana_admin, ["retention-rules-apply"])
    assert result.exit_code == 0

    for file in to_be_kept:
        assert workspace.joinpath(file).exists()
    for file in to_be_deleted:
        assert not workspace.joinpath(file).exists()


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

    def test_check_correct_created_session(self, session, sample_serial_workflow_in_db):
        from reana_db.models import InteractiveSession

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
        from reana_db.models import InteractiveSession

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
        from reana_db.models import InteractiveSession

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
        from reana_db.models import InteractiveSession

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
