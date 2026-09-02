# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test server views."""

import copy
import json
import logging
import os
import shutil
import zipfile
import yaml
from contextlib import contextmanager
from io import BytesIO
from uuid import uuid4

import pytest
from flask import Flask, url_for
from mock import Mock, patch
from werkzeug.test import EnvironBuilder
from reana_commons.config import (
    WORKFLOW_RUNTIME_USER_GID,
    WORKFLOW_RUNTIME_USER_UID,
)
from reana_commons.testing import make_mock_api_client

from reana_db.models import (
    User,
    InteractiveSessionType,
    RunStatus,
    UserWorkflow,
    Workflow,
    WorkflowResource,
    WorkspaceRetentionAuditLog,
    WorkspaceRetentionRule,
    WorkspaceRetentionRuleStatus,
)
from reana_commons.errors import (
    REANAQuotaExceededError,
    REANASpecificationPathError,
    REANAValidationError,
)
from reana_commons.k8s.secrets import UserSecrets, Secret

from reana_server.utils import (
    _create_and_associate_local_user,
    _create_and_associate_oauth_user,
)
from reana_server.workspace_mutations import (
    WorkspaceMutationConflict,
    WorkspaceMutationUnavailable,
)
from reana_server.validation import SpecValidationServiceError


@pytest.fixture(autouse=True)
def _validation_shared_volume(app, monkeypatch):
    """Stage validation snapshots in the test application's shared volume."""
    monkeypatch.setattr(
        "reana_server.rest.workflows.SHARED_VOLUME_PATH",
        app.config["SHARED_VOLUME_PATH"],
    )
    monkeypatch.setattr(
        "reana_server.rest.launch.SHARED_VOLUME_PATH",
        app.config["SHARED_VOLUME_PATH"],
    )


def test_get_workflows(app, user0, _get_user_mock):
    """Test get_workflows view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(
                url_for("workflows.get_workflows"),
                query_string={"type": "batch"},
            )
            assert res.status_code == 401

            res = client.get(
                url_for("workflows.get_workflows"),
                query_string={"access_token": "wrongtoken", "type": "batch"},
            )
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.get_workflows"),
                query_string={
                    "access_token": user0.access_token,
                    "type": "batch",
                },
            )
            assert res.status_code == 200


SERIAL_REANA_YAML = (
    "workflow:\n"
    "  type: serial\n"
    "  specification:\n"
    "    steps:\n"
    "      - name: step1\n"
    "        environment: 'docker.io/library/busybox:1.36'\n"
    "        commands:\n"
    "          - echo hello\n"
    "inputs:\n"
    "  parameters: {}\n"
)


def _serial_bundle():
    """Build a fresh multipart spec bundle for a single request."""
    return _zip_bundle({"reana.yaml": SERIAL_REANA_YAML.encode()})


def _serial_bundle_with_uid_override():
    """Build a spec using one image under two runtime identities."""
    reana_yaml = (
        "workflow:\n"
        "  type: serial\n"
        "  specification:\n"
        "    steps:\n"
        "      - name: default-uid\n"
        "        environment: 'docker.io/library/busybox:1.36'\n"
        "        commands: ['true']\n"
        "      - name: custom-uid\n"
        "        environment: 'docker.io/library/busybox:1.36'\n"
        "        kubernetes_uid: 2000\n"
        "        commands: ['true']\n"
        "inputs:\n"
        "  parameters: {}\n"
    )
    return _zip_bundle({"reana.yaml": reana_yaml.encode()})


def _bundle_with_spec(spec):
    """Build a fresh multipart bundle containing raw specification text."""
    return _zip_bundle({"reana.yaml": spec.encode()})


def _zip_bundle(entries):
    """Build the one-file multipart validation ZIP contract."""
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(entries.items()):
            archive.writestr(name, content)
    stream.seek(0)
    return {"bundle": (stream, "validation-bundle.zip")}


def _quota_call_for_user(mock, user):
    """Assert a quota helper was called once for ``user`` and return the call.

    The request-scoped ``user`` the view passes is a different SQLAlchemy
    instance from the test fixture (loaded in a separate session), so compare by
    the stable ``id_`` rather than object identity.
    """
    mock.assert_called_once()
    call = mock.call_args
    assert call.args[0].id_ == user.id_
    return call


def test_create_workflow(
    app,
    session,
    user0,
    _get_user_mock,
    sample_serial_workflow_in_db,
    monkeypatch,
    tmp_path,
):
    """Test create_workflow view (multipart specification bundle upload)."""
    monkeypatch.setattr(
        "reana_server.rest.workflows.uuid.uuid4",
        lambda: sample_serial_workflow_in_db.id_,
    )
    monkeypatch.setattr(
        "reana_server.rest.workflows.build_workspace_path",
        lambda *_args: sample_serial_workflow_in_db.workspace_path,
    )
    # The bundle is staged on the shared volume before being loaded/validated.
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    # The server seeds the freshly created workspace from the uploaded bundle, so
    # the mocked controller must return a real workflow whose workspace exists.
    create_http_response = Mock()
    create_http_response.status_code = 200
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {
            "workflow_id": str(sample_serial_workflow_in_db.id_),
            "workflow_name": sample_serial_workflow_in_db.name,
        },
        create_http_response,
    )
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            rwc_client,
        ), patch(
            "reana_server.rest.workflows.prevent_disk_quota_excess"
        ) as prevent_quota_mock, patch(
            "reana_server.rest.workflows.store_workflow_disk_quota"
        ) as store_workflow_quota_mock, patch(
            "reana_server.rest.workflows.update_users_disk_quota"
        ) as update_user_quota_mock:
            res = client.post(url_for("workflows.create_workflow"))
            assert res.status_code == 401

            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={
                    "access_token": "wrongtoken",
                },
            )
            assert res.status_code == 403

            # remote repository given as spec, not implemented
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={
                    "access_token": user0.access_token,
                    "spec": "not_implemented",
                },
            )
            assert res.status_code == 501

            # no specification bundle provided
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={"access_token": user0.access_token},
            )
            assert res.status_code == 400

            # name cannot be valid uuid4
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={
                    "access_token": user0.access_token,
                    "workflow_name": str(uuid4()),
                },
                data=_serial_bundle(),
                content_type="multipart/form-data",
            )
            assert res.status_code == 400

            # correct case: a serial bundle is loaded and validated in-process,
            # then seeded into the created workspace (C1) and the staging dir is
            # cleaned up. The uploaded bytes are pre-checked against disk quota and
            # accounted after the workspace is seeded.
            prevent_quota_mock.reset_mock()
            store_workflow_quota_mock.reset_mock()
            update_user_quota_mock.reset_mock()
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={
                    "access_token": user0.access_token,
                    "workflow_name": "test",
                },
                data=_serial_bundle(),
                content_type="multipart/form-data",
            )
            assert res.status_code == 200
            bundle_bytes = len(SERIAL_REANA_YAML.encode())
            prevent_call = _quota_call_for_user(prevent_quota_mock, user0)
            assert prevent_call.args[1] == bundle_bytes
            assert prevent_call.kwargs == {"action": "Creating the workflow test"}
            seeded = os.path.join(
                sample_serial_workflow_in_db.workspace_path, "reana.yaml"
            )
            assert os.path.isfile(seeded)
            store_workflow_quota_mock.assert_called_once_with(
                sample_serial_workflow_in_db, bytes_to_sum=bundle_bytes
            )
            update_call = _quota_call_for_user(update_user_quota_mock, user0)
            assert update_call.kwargs == {"bytes_to_sum": bundle_bytes}
            # No staging bundle is left behind under the shared volume.
            staging = os.path.join(str(tmp_path), "validation-tmp")
            assert not os.path.isdir(staging) or not os.listdir(staging)


def test_create_workflow_rejects_legacy_json_specification(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """A released client's JSON create gets actionable upgrade guidance."""
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    rwc_client = Mock()
    with app.test_client() as client, patch(
        "reana_server.rest.workflows.current_rwc_api_client", rwc_client
    ):
        # This is the exact wire shape a released client sends: the OpenAPI body
        # parameter is *named* ``reana_specification``, but a Swagger 2.0 body
        # parameter's schema is the body, so the serialized specification
        # arrives as the whole JSON document with no wrapping key.
        res = client.post(
            url_for("workflows.create_workflow"),
            query_string={
                "access_token": user0.access_token,
                "workflow_name": "test",
            },
            data=json.dumps(
                {
                    "version": "0.3.0",
                    "inputs": {"files": ["code/helloworld.py"]},
                    "outputs": {"files": ["results/greetings.txt"]},
                    "workflow": {
                        "type": "serial",
                        "specification": {"steps": [{"commands": ["echo hello"]}]},
                    },
                }
            ),
            content_type="application/json",
        )

    assert res.status_code == 400
    assert "reana_specification" in res.json["message"]
    assert "upgrade REANA client" in res.json["message"]
    assert "'bundle'" in res.json["message"]
    rwc_client.api.create_workflow.assert_not_called()


@pytest.mark.parametrize(
    "lock_error,status_code",
    [(WorkspaceMutationConflict, 409), (WorkspaceMutationUnavailable, 503)],
)
def test_create_workflow_maps_creation_lock_failures(
    app, user0, _get_user_mock, monkeypatch, tmp_path, lock_error, status_code
):
    """Creation exposes family-lock contention and infrastructure failures."""
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    with app.test_client() as client, patch(
        "reana_server.rest.workflows.workflow_creation_mutation_lock",
        side_effect=lock_error(),
    ):
        response = client.post(
            url_for("workflows.create_workflow"),
            query_string={
                "access_token": user0.access_token,
                "workflow_name": "locked-create",
            },
            data=_serial_bundle(),
            content_type="multipart/form-data",
        )

    assert response.status_code == status_code
    assert response.json["message"]


def test_create_workflow_compensates_an_unexpected_controller_id(
    app,
    session,
    user0,
    _get_user_mock,
    sample_serial_workflow_in_db,
    monkeypatch,
    tmp_path,
):
    """A controller that ignores the reserved UUID cannot leave an orphan."""
    expected_workflow_id = uuid4()
    monkeypatch.setattr(
        "reana_server.rest.workflows.uuid.uuid4", lambda: expected_workflow_id
    )
    monkeypatch.setattr(
        "reana_server.rest.workflows.build_workspace_path",
        lambda *_args: sample_serial_workflow_in_db.workspace_path,
    )
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {"workflow_id": str(sample_serial_workflow_in_db.id_)},
        Mock(status_code=201),
    )

    with app.test_client() as client, patch(
        "reana_server.rest.workflows.current_rwc_api_client", rwc_client
    ), patch("reana_server.rest.workflows.prevent_disk_quota_excess"), patch(
        "reana_server.rest.workflows.store_workflow_disk_quota"
    ), patch(
        "reana_server.rest.workflows.update_users_disk_quota"
    ):
        response = client.post(
            url_for("workflows.create_workflow"),
            query_string={
                "access_token": user0.access_token,
                "workflow_name": "mismatched-create",
            },
            data=_serial_bundle(),
            content_type="multipart/form-data",
        )

    assert response.status_code == 500
    session.refresh(sample_serial_workflow_in_db)
    assert sample_serial_workflow_in_db.status == RunStatus.deleted
    assert not os.path.exists(sample_serial_workflow_in_db.workspace_path)


def test_create_workflow_rejects_over_quota_user_before_staging(
    app, session, user0, _get_user_mock, monkeypatch, tmp_path
):
    """An over-quota raw-bundle create is rejected before any expensive work.

    The quota guard runs before staging the bundle or spawning a validator Job,
    so nothing is staged/validated and no controller create (hence no orphan
    workflow) happens.
    """
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    rwc_client = Mock()
    with app.test_client() as client, patch(
        "reana_server.rest.workflows.current_rwc_api_client", rwc_client
    ), patch("reana_db.models.User.has_exceeded_quota", return_value=True), patch(
        "reana_server.rest.workflows.get_quota_excess_message",
        return_value="quota exceeded",
    ), patch(
        "reana_server.rest.workflows._stage_validation_bundle"
    ) as stage_mock, patch(
        "reana_server.rest.workflows.load_and_validate_spec"
    ) as load_mock:
        res = client.post(
            url_for("workflows.create_workflow"),
            query_string={
                "access_token": user0.access_token,
                "workflow_name": "over-quota",
            },
            data=_serial_bundle(),
            content_type="multipart/form-data",
        )
    assert res.status_code == 403
    stage_mock.assert_not_called()
    load_mock.assert_not_called()
    rwc_client.api.create_workflow.assert_not_called()


def test_create_workflow_quota_excess_before_create_leaves_no_orphan(
    app, session, user0, _get_user_mock, monkeypatch, tmp_path
):
    """A staged bundle that would exceed quota fails before the row is created.

    ``prevent_disk_quota_excess`` runs after staging/validation but before the
    controller create, so a rejection returns 403 with no orphan workflow and
    the staging directory is cleaned up.
    """
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    rwc_client = Mock()
    with app.test_client() as client, patch(
        "reana_server.rest.workflows.current_rwc_api_client", rwc_client
    ), patch(
        "reana_server.rest.workflows.prevent_disk_quota_excess",
        side_effect=REANAQuotaExceededError("disk quota exceeded"),
    ):
        res = client.post(
            url_for("workflows.create_workflow"),
            query_string={
                "access_token": user0.access_token,
                "workflow_name": "quota-excess",
            },
            data=_serial_bundle(),
            content_type="multipart/form-data",
        )
    assert res.status_code == 403
    rwc_client.api.create_workflow.assert_not_called()
    staging = os.path.join(str(tmp_path), "validation-tmp")
    assert not os.path.isdir(staging) or not os.listdir(staging)


def test_raw_bundle_create_failure_compensates_workspace_and_quota(
    app,
    session,
    user0,
    _get_user_mock,
    sample_serial_workflow_in_db,
    monkeypatch,
    tmp_path,
):
    """A failure after controller create leaves no live workflow or workspace.

    Exercise the latest possible failure point: workflow quota accounting and
    bundle promotion have completed, but user quota accounting fails. Cleanup
    must remove the promoted workspace, mark the row deleted, reset workflow
    quota, and recalculate user quota. Calling compensation again demonstrates
    that every cleanup action is retry-safe.
    """
    monkeypatch.setattr(
        "reana_server.rest.workflows.uuid.uuid4",
        lambda: sample_serial_workflow_in_db.id_,
    )
    monkeypatch.setattr(
        "reana_server.rest.workflows.build_workspace_path",
        lambda *_args: sample_serial_workflow_in_db.workspace_path,
    )
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    create_http_response = Mock(status_code=201)
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {
            "workflow_id": str(sample_serial_workflow_in_db.id_),
            "workflow_name": sample_serial_workflow_in_db.name,
        },
        create_http_response,
    )

    def _fail_initial_user_accounting(*args, **kwargs):
        if "bytes_to_sum" in kwargs:
            raise RuntimeError("private quota database detail")

    with app.test_client() as client, patch(
        "reana_server.rest.workflows.current_rwc_api_client", rwc_client
    ), patch("reana_server.rest.workflows.prevent_disk_quota_excess"), patch(
        "reana_server.rest.workflows.store_workflow_disk_quota"
    ) as store_mock, patch(
        "reana_server.rest.workflows.update_users_disk_quota",
        side_effect=_fail_initial_user_accounting,
    ) as update_mock:
        res = client.post(
            url_for("workflows.create_workflow"),
            query_string={
                "access_token": user0.access_token,
                "workflow_name": "transactional-create",
            },
            data=_serial_bundle(),
            content_type="multipart/form-data",
        )

        assert res.status_code == 500
        assert res.json["message"] == "An internal server error occurred."
        assert "private quota database detail" not in res.get_data(as_text=True)

        session.refresh(sample_serial_workflow_in_db)
        assert sample_serial_workflow_in_db.status == RunStatus.deleted
        assert not os.path.exists(sample_serial_workflow_in_db.workspace_path)

        bundle_bytes = len(SERIAL_REANA_YAML.encode())
        assert store_mock.call_args_list[0].kwargs == {"bytes_to_sum": bundle_bytes}
        assert store_mock.call_args_list[1].kwargs == {
            "bytes_to_sum": None,
            "override_policy_checks": True,
        }
        assert update_mock.call_args_list[0].kwargs == {"bytes_to_sum": bundle_bytes}
        assert update_mock.call_args_list[1].kwargs == {"override_policy_checks": True}

        # A retried compensating action converges on the same state.
        from reana_server.rest.workflows import _compensate_failed_workflow_create

        _compensate_failed_workflow_create(sample_serial_workflow_in_db, user0)
        assert not os.path.exists(sample_serial_workflow_in_db.workspace_path)
        assert store_mock.call_args_list[-1].kwargs == {
            "bytes_to_sum": None,
            "override_policy_checks": True,
        }
        assert update_mock.call_args_list[-1].kwargs == {"override_policy_checks": True}


def test_raw_bundle_promotion_failure_is_compensated(
    app,
    session,
    user0,
    _get_user_mock,
    sample_serial_workflow_in_db,
    monkeypatch,
    tmp_path,
):
    """Even a partial workspace promotion is removed before returning 500."""
    monkeypatch.setattr(
        "reana_server.rest.workflows.uuid.uuid4",
        lambda: sample_serial_workflow_in_db.id_,
    )
    monkeypatch.setattr(
        "reana_server.rest.workflows.build_workspace_path",
        lambda *_args: sample_serial_workflow_in_db.workspace_path,
    )
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {
            "workflow_id": str(sample_serial_workflow_in_db.id_),
            "workflow_name": sample_serial_workflow_in_db.name,
        },
        Mock(status_code=201),
    )

    def _partially_promote_then_fail(source, target):
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "partial"), "w") as partial:
            partial.write("partial")
        raise RuntimeError("private filesystem detail")

    with app.test_client() as client, patch(
        "reana_server.rest.workflows.current_rwc_api_client", rwc_client
    ), patch("reana_server.rest.workflows.prevent_disk_quota_excess"), patch(
        "reana_server.rest.workflows.mv_workflow_files",
        side_effect=_partially_promote_then_fail,
    ), patch(
        "reana_server.rest.workflows.store_workflow_disk_quota"
    ) as store_mock, patch(
        "reana_server.rest.workflows.update_users_disk_quota"
    ) as update_mock:
        res = client.post(
            url_for("workflows.create_workflow"),
            query_string={
                "access_token": user0.access_token,
                "workflow_name": "failed-promotion",
            },
            data=_serial_bundle(),
            content_type="multipart/form-data",
        )

    assert res.status_code == 500
    assert "private filesystem detail" not in res.get_data(as_text=True)
    session.refresh(sample_serial_workflow_in_db)
    assert sample_serial_workflow_in_db.status == RunStatus.deleted
    assert not os.path.exists(sample_serial_workflow_in_db.workspace_path)
    store_mock.assert_called_once_with(
        sample_serial_workflow_in_db,
        bytes_to_sum=None,
        override_policy_checks=True,
    )
    update_mock.assert_called_once()
    assert update_mock.call_args.kwargs == {"override_policy_checks": True}


def _gitlab_create_patches(
    rwc_client, tmp_path, validation_result=None, validation_error=None
):
    """Common mocks for driving the GitLab branch of ``create_workflow``."""
    spec = yaml.safe_load(SERIAL_REANA_YAML)
    fetched_dir = tmp_path / "gitlab-source"
    fetched_dir.mkdir()
    specification_path = fetched_dir / "reana.yaml"
    specification_path.write_text(SERIAL_REANA_YAML)
    fetcher = Mock()
    fetcher.workflow_spec_path.return_value = str(specification_path)
    gitlab_client = Mock()
    gitlab_client.get_repository_archive.return_value = Mock()
    validation_patch = patch(
        "reana_server.rest.workflows.load_and_validate_spec",
        return_value=validation_result or (spec, []),
        side_effect=validation_error,
    )
    return spec, [
        patch("reana_server.rest.workflows.current_rwc_api_client", rwc_client),
        patch("reana_db.models.User.has_exceeded_quota", return_value=False),
        patch(
            "reana_server.rest.workflows._get_reana_yaml_from_gitlab",
            return_value=(
                spec,
                "https://gitlab.example/x",
                "gl-wf",
                "main",
                "deadbeef",
            ),
        ),
        patch(
            "reana_server.rest.workflows.get_fetched_workflows_dir",
            return_value=str(fetched_dir),
        ),
        patch(
            "reana_server.rest.workflows.GitLabClient.from_k8s_secret",
            return_value=gitlab_client,
        ),
        patch(
            "reana_server.rest.workflows.extract_streamed_zip_response",
            return_value=fetcher,
        ),
        validation_patch,
        patch(
            "reana_server.rest.workflows.workspace_seed_members",
            return_value=({"reana.yaml": str(specification_path)}, 123),
        ),
        patch("reana_server.rest.workflows.seed_workspace", return_value=123),
    ]


def test_create_workflow_gitlab_accounts_disk_quota(
    app,
    session,
    user0,
    _get_user_mock,
    sample_serial_workflow_in_db,
    monkeypatch,
    tmp_path,
):
    """A successful GitLab create charges the cloned workspace to disk quota."""
    monkeypatch.setattr(
        "reana_server.rest.workflows.uuid.uuid4",
        lambda: sample_serial_workflow_in_db.id_,
    )
    monkeypatch.setattr(
        "reana_server.rest.workflows.build_workspace_path",
        lambda *_args: sample_serial_workflow_in_db.workspace_path,
    )
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {
            "workflow_id": str(sample_serial_workflow_in_db.id_),
            "workflow_name": sample_serial_workflow_in_db.name,
        },
        Mock(status_code=200),
    )
    _spec, patches = _gitlab_create_patches(rwc_client, tmp_path)
    with app.test_client() as client:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[
            5
        ], patches[6], patches[7], patches[8], patch(
            "reana_server.rest.workflows.publish_workflow_submission"
        ), patch(
            "reana_server.rest.workflows.store_workflow_disk_quota"
        ) as store_mock, patch(
            "reana_server.rest.workflows.update_users_disk_quota"
        ) as update_mock:
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={"access_token": user0.access_token},
                data=json.dumps({"object_kind": "push"}),
                content_type="application/json",
            )
    assert res.status_code == 200
    store_mock.assert_called_once_with(sample_serial_workflow_in_db, bytes_to_sum=123)
    update_call = _quota_call_for_user(update_mock, user0)
    assert update_call.kwargs == {"bytes_to_sum": 123}


def test_create_workflow_gitlab_quota_excess_rolls_back(
    app,
    session,
    user0,
    _get_user_mock,
    sample_serial_workflow_in_db,
    monkeypatch,
    tmp_path,
):
    """A GitLab snapshot exceeding quota is rejected before controller create."""
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {
            "workflow_id": str(sample_serial_workflow_in_db.id_),
            "workflow_name": sample_serial_workflow_in_db.name,
        },
        Mock(status_code=200),
    )
    _spec, patches = _gitlab_create_patches(rwc_client, tmp_path)
    with app.test_client() as client:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[
            5
        ], patches[6], patches[7], patches[8], patch(
            "reana_server.rest.workflows.prevent_disk_quota_excess",
            side_effect=REANAQuotaExceededError("disk quota exceeded"),
        ), patch(
            "reana_server.rest.workflows._fail_gitlab_commit_build_status"
        ) as fail_build_mock, patch(
            "reana_server.rest.workflows.store_workflow_disk_quota"
        ) as store_mock, patch(
            "reana_server.rest.workflows.update_users_disk_quota"
        ) as update_mock:
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={"access_token": user0.access_token},
                data=json.dumps({"object_kind": "push"}),
                content_type="application/json",
            )
    assert res.status_code == 200  # GitLab webhook acknowledged
    session.refresh(sample_serial_workflow_in_db)
    assert sample_serial_workflow_in_db.status == RunStatus.created
    rwc_client.api.create_workflow.assert_not_called()
    fail_build_mock.assert_called_once()
    store_mock.assert_not_called()
    update_mock.assert_not_called()


def test_create_workflow_gitlab_invalid_spec_leaves_no_orphan(
    app,
    session,
    user0,
    _get_user_mock,
    sample_serial_workflow_in_db,
    monkeypatch,
    tmp_path,
):
    """An invalid GitLab snapshot is rejected before creating a workflow row."""
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {
            "workflow_id": str(sample_serial_workflow_in_db.id_),
            "workflow_name": sample_serial_workflow_in_db.name,
        },
        Mock(status_code=200),
    )
    _spec, patches = _gitlab_create_patches(
        rwc_client,
        tmp_path,
        validation_error=REANAValidationError("invalid cloned specification"),
    )
    with app.test_client() as client:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[
            5
        ], patches[6], patches[7], patches[8], patch(
            "reana_server.rest.workflows.store_workflow_disk_quota"
        ) as store_mock, patch(
            "reana_server.rest.workflows.update_users_disk_quota"
        ) as update_mock, patch(
            "reana_server.rest.workflows.publish_workflow_submission"
        ) as publish_mock:
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={"access_token": user0.access_token},
                data=json.dumps({"object_kind": "push"}),
                content_type="application/json",
            )
    assert res.status_code == 400
    session.refresh(sample_serial_workflow_in_db)
    assert sample_serial_workflow_in_db.status == RunStatus.created
    rwc_client.api.create_workflow.assert_not_called()
    store_mock.assert_not_called()
    update_mock.assert_not_called()
    publish_mock.assert_not_called()


def test_create_workflow_gitlab_surfaces_validation_warnings(
    app,
    session,
    user0,
    _get_user_mock,
    sample_serial_workflow_in_db,
    monkeypatch,
    tmp_path,
):
    """Post-create GitLab validation warnings are surfaced, not dropped.

    On a successful GitLab create the advisory warnings from the post-create
    ``load_and_validate_spec`` must be returned to the caller under
    ``validation_warnings``.
    """
    monkeypatch.setattr(
        "reana_server.rest.workflows.uuid.uuid4",
        lambda: sample_serial_workflow_in_db.id_,
    )
    monkeypatch.setattr(
        "reana_server.rest.workflows.build_workspace_path",
        lambda *_args: sample_serial_workflow_in_db.workspace_path,
    )
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {
            "workflow_id": str(sample_serial_workflow_in_db.id_),
            "workflow_name": sample_serial_workflow_in_db.name,
        },
        Mock(status_code=200),
    )
    warning = {"code": "environment", "message": "floating tag", "path": "step1"}
    _spec, patches = _gitlab_create_patches(
        rwc_client,
        tmp_path,
        validation_result=(yaml.safe_load(SERIAL_REANA_YAML), [warning]),
    )
    with app.test_client() as client:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[
            5
        ], patches[6], patches[7], patches[8], patch(
            "reana_server.rest.workflows.store_workflow_disk_quota"
        ), patch(
            "reana_server.rest.workflows.update_users_disk_quota"
        ), patch(
            "reana_server.rest.workflows.publish_workflow_submission"
        ):
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={"access_token": user0.access_token},
                data=json.dumps({"object_kind": "push"}),
                content_type="application/json",
            )
    assert res.status_code == 200
    assert warning in res.json["validation_warnings"]


def test_launch_validates_definition_before_seeding_inputs(
    app,
    user0,
    _get_user_mock,
    sample_serial_workflow_in_db,
    monkeypatch,
    tmp_path,
):
    """Launch validates definitions only, then seeds declared datasets."""
    monkeypatch.setattr(
        "reana_server.rest.launch.uuid.uuid4",
        lambda: sample_serial_workflow_in_db.id_,
    )
    monkeypatch.setattr(
        "reana_server.rest.launch.build_workspace_path",
        lambda *_args: sample_serial_workflow_in_db.workspace_path,
    )
    source = tmp_path / "launch-source"
    (source / "code").mkdir(parents=True)
    (source / "data").mkdir()
    (source / "code" / "helper.py").write_text("print('helper')")
    (source / "data" / "input.csv").write_text("dataset")
    (source / "undeclared.txt").write_text("must not enter workspace")
    (source / "reana.yaml").write_text(
        "inputs:\n"
        "  files: [data/input.csv]\n"
        "  parameters: {}\n"
        "workflow:\n"
        "  type: serial\n"
        "  files: [code/helper.py]\n"
        "  specification:\n"
        "    steps:\n"
        "      - name: step1\n"
        "        environment: docker.io/library/busybox:1.36\n"
        "        commands: ['true']\n"
    )
    fetcher = Mock()
    fetcher.workflow_spec_path.return_value = str(source / "reana.yaml")
    fetcher.generate_workflow_name.return_value = "launched"
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {
            "workflow_id": str(sample_serial_workflow_in_db.id_),
            "workflow_name": sample_serial_workflow_in_db.name,
        },
        Mock(status_code=201),
    )
    shutil.rmtree(sample_serial_workflow_in_db.workspace_path)

    from reana_server.validation import load_and_validate_spec

    validated_members = set()

    def record_validation_snapshot(directory):
        for root, _directories, files in os.walk(directory):
            for filename in files:
                validated_members.add(
                    os.path.relpath(os.path.join(root, filename), directory)
                )
        return load_and_validate_spec(directory)

    with app.test_client() as client, patch(
        "reana_server.rest.launch.get_fetched_workflows_dir",
        return_value=str(source),
    ), patch("reana_server.rest.launch.get_fetcher", return_value=fetcher), patch(
        "reana_server.rest.launch.load_and_validate_spec",
        side_effect=record_validation_snapshot,
    ), patch(
        "reana_server.rest.launch.current_rwc_api_client", rwc_client
    ), patch(
        "reana_server.rest.launch.prevent_disk_quota_excess"
    ), patch(
        "reana_server.rest.launch.store_workflow_disk_quota"
    ), patch(
        "reana_server.rest.launch.update_users_disk_quota"
    ), patch(
        "reana_server.rest.launch.publish_workflow_submission"
    ):
        response = client.post(
            url_for("launch.launch"),
            query_string={"access_token": user0.access_token},
            json={"url": "https://example.org/workflow.zip"},
        )

    assert response.status_code == 200
    assert validated_members == {"reana.yaml", "code/helper.py"}
    workspace = sample_serial_workflow_in_db.workspace_path
    assert os.path.isfile(os.path.join(workspace, "reana.yaml"))
    assert os.path.isfile(os.path.join(workspace, "code", "helper.py"))
    assert os.path.isfile(os.path.join(workspace, "data", "input.csv"))
    assert not os.path.exists(os.path.join(workspace, "undeclared.txt"))
    create_call = rwc_client.api.create_workflow.call_args
    assert create_call.kwargs["workflow"]["workflow_id"] == str(
        sample_serial_workflow_in_db.id_
    )
    assert set(create_call.kwargs["_request_options"]) == {"connect_timeout", "timeout"}


def test_validation_and_start_endpoints_use_slow_rate_limit(app):
    """Validation, creation, and start carry the slow rate limit.

    These endpoints can spawn a sandboxed validation Job per call, so they must
    be throttled like ``launch`` rather than falling under the fast global
    authenticated-user limit.
    """
    from flask import request
    from invenio_app.limiter import set_rate_limit
    from reana_server.config import REANA_RATELIMIT_SLOW, set_reana_rate_limit

    for endpoint in (
        "workflows.validate_workflow_specification",
        "workflows.create_workflow",
        "workflows.start_workflow",
        "workflows.restart_workflow",
    ):
        values = (
            {"workflow_id_or_name": "test"}
            if endpoint in ("workflows.start_workflow", "workflows.restart_workflow")
            else {}
        )
        with app.test_request_context(url_for(endpoint, **values), method="POST"):
            assert request.endpoint == endpoint
            assert set_reana_rate_limit() == REANA_RATELIMIT_SLOW

    status_url = url_for("workflows.set_workflow_status", workflow_id_or_name="test")
    with app.test_request_context(
        status_url, method="PUT", query_string={"status": "start"}
    ):
        assert set_reana_rate_limit() == REANA_RATELIMIT_SLOW

    for status in ("stop", "deleted"):
        with app.test_request_context(
            status_url, method="PUT", query_string={"status": status}
        ):
            assert set_reana_rate_limit() == set_rate_limit()


def test_validate_workflow_specification_environment_check(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """The ``environments`` flag drives the optional image check wiring.

    Offline tag checks run only when requested and the endpoint returns image
    and effective runtime-identity records for an optional local ``--pull``.
    """
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    finding = {"code": "image_tag", "message": "boom", "path": "img:1"}
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.iter_environment_tag_warnings",
            return_value=iter(
                [{"code": "image_tag", "message": "boom", "image": "img:1"}]
            ),
        ) as env_mock:
            # Without the flag the check is not run and no image data is added.
            res = client.post(
                url_for("workflows.validate_workflow_specification"),
                query_string={"access_token": user0.access_token},
                data=_serial_bundle_with_uid_override(),
                content_type="multipart/form-data",
            )
            assert res.status_code == 200
            env_mock.assert_not_called()
            assert "images" not in res.json
            assert "environments" not in res.json
            assert "reana_specification" not in res.json

            # --environments returns offline findings and image identities.
            res = client.post(
                url_for("workflows.validate_workflow_specification"),
                query_string={
                    "access_token": user0.access_token,
                    "environments": "true",
                },
                data=_serial_bundle_with_uid_override(),
                content_type="multipart/form-data",
            )
            assert res.status_code == 200
            env_mock.assert_called_once()
            assert finding in res.json["warnings"]
            assert res.json["environments"] == [
                {
                    "image": "docker.io/library/busybox:1.36",
                    "runtime_uid": int(WORKFLOW_RUNTIME_USER_UID),
                    "runtime_gid": int(WORKFLOW_RUNTIME_USER_GID),
                },
                {
                    "image": "docker.io/library/busybox:1.36",
                    "runtime_uid": 2000,
                    "runtime_gid": int(WORKFLOW_RUNTIME_USER_GID),
                },
            ]
            assert "images" not in res.json
            assert "runtime_uid" not in res.json
            assert "runtime_gid" not in res.json
            assert "reana_specification" not in res.json


def test_environment_response_uses_one_bounded_lazy_budget():
    """High-cardinality environments cannot create an oversized response."""
    from reana_server.rest.workflows import _add_bounded_environments

    yielded = 0

    def environments(*_args):
        nonlocal yielded
        for index in range(100_000):
            yielded += 1
            yield {
                "image": "registry.example/image-{}:latest".format(index),
                "runtime_uid": 1000,
                "runtime_gid": 1000,
            }

    report = {"valid": True, "errors": [], "warnings": []}
    with patch("reana_server.rest.workflows.iter_image_environments", environments):
        _add_bounded_environments(report, {"workflow": {"type": "serial"}})

    assert yielded == 101
    assert len(report["environments"]) == 99
    assert report["warnings"] == [
        {
            "code": "report_truncated",
            "message": "Additional validation findings were omitted.",
            "path": "",
        }
    ]
    assert report["environments_truncated"] is True
    assert len(json.dumps(report).encode()) < 16 * 1024 * 1024


def test_environment_response_skips_only_overlong_image_identity():
    """An overlong image does not suppress later identities or tag findings."""
    from reana_server.rest.workflows import _add_bounded_environments

    report = {"valid": True, "errors": [], "warnings": []}
    environments = [
        {"image": "safe-before:latest", "runtime_uid": 1000, "runtime_gid": 1000},
        {
            "image": "registry.example/" + "x" * 600,
            "runtime_uid": 1000,
            "runtime_gid": 1000,
        },
        {"image": "safe-after:latest", "runtime_uid": 1000, "runtime_gid": 1000},
    ]
    with patch(
        "reana_server.rest.workflows.iter_image_environments",
        return_value=iter(environments),
    ), patch(
        "reana_server.rest.workflows.iter_environment_tag_warnings",
        return_value=iter(
            [
                {
                    "code": "latest",
                    "message": "safe tag warning",
                    "image": "safe-after:latest",
                }
            ]
        ),
    ):
        _add_bounded_environments(report, {"workflow": {"type": "serial"}})

    assert [item["image"] for item in report["environments"]] == [
        "safe-before:latest",
        "safe-after:latest",
    ]
    assert report["environments_truncated"] is True
    assert {warning["code"] for warning in report["warnings"]} == {
        "environment_identity_omitted",
        "latest",
        "report_truncated",
    }


def test_environment_budget_preserves_existing_validation_warnings():
    """Truncation drops lower-priority environments before prior warnings."""
    from reana_server.rest.workflows import _add_bounded_environments

    original_warnings = [
        {"code": "validation", "message": str(index), "path": ""} for index in range(99)
    ]
    report = {"valid": True, "errors": [], "warnings": list(original_warnings)}
    environments = iter(
        [
            {"image": "image:1", "runtime_uid": 1000, "runtime_gid": 1000},
            {"image": "image:2", "runtime_uid": 1000, "runtime_gid": 1000},
        ]
    )
    with patch(
        "reana_server.rest.workflows.iter_image_environments",
        return_value=environments,
    ):
        _add_bounded_environments(report, {"workflow": {"type": "serial"}})

    assert report["warnings"][:-1] == original_warnings
    assert report["warnings"][-1]["code"] == "report_truncated"
    assert report["environments"] == []
    assert report["environments_truncated"] is True


def test_environment_marker_eviction_sets_truncated_flag():
    """Reserving the marker reports an environment evicted at the boundary."""
    from reana_server.rest.workflows import _add_bounded_environments

    original_warnings = [
        {"code": "validation", "message": str(index), "path": ""} for index in range(99)
    ]
    report = {"valid": True, "errors": [], "warnings": list(original_warnings)}
    environment = {
        "image": "registry.example/image:latest",
        "runtime_uid": 1000,
        "runtime_gid": 1000,
    }
    with patch(
        "reana_server.rest.workflows.iter_image_environments",
        return_value=iter([environment]),
    ):
        _add_bounded_environments(report, {"workflow": {"type": "serial"}})

    assert report["warnings"][:-1] == original_warnings
    assert report["warnings"][-1]["code"] == "report_truncated"
    assert report["environments"] == []
    assert report["environments_truncated"] is True


def test_validate_workflow_specification_internal_error_returns_500(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """A sandbox internal/infra failure surfaces as 500, not 200 valid:false.

    When the sandbox validator exits with the internal-error code (or reports an
    ``internal``-coded error) ``validate_spec_bundle`` raises
    ``SpecValidationServiceError``. The endpoint must map that to a 500 service
    error -- never to a 200 ``valid:false`` report that a client would render as
    "X is not a valid REANA specification".
    """
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.validate_spec_bundle",
            side_effect=SpecValidationServiceError("internal validation error"),
        ):
            res = client.post(
                url_for("workflows.validate_workflow_specification"),
                query_string={"access_token": user0.access_token},
                data=_serial_bundle(),
                content_type="multipart/form-data",
            )
    assert res.status_code == 500
    # Reported as a service failure, not an invalid specification.
    assert res.json.get("valid") is not False
    assert res.json["message"] == "An internal server error occurred."
    assert "internal validation error" not in res.get_data(as_text=True)


def test_validate_workflow_specification_controller_unreachable_returns_500(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """A controller outage during validation surfaces as 500, not a 400.

    Controller-unreachable / non-OK controller responses raise
    ``SpecValidationServiceError`` from ``_call_rwc_validate``; the endpoint must
    treat that server-side outage as a 500, not a 400 client error.
    """
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.validate_spec_bundle",
            side_effect=SpecValidationServiceError(
                "Could not reach the workflow specification validation service."
            ),
        ):
            res = client.post(
                url_for("workflows.validate_workflow_specification"),
                query_string={"access_token": user0.access_token},
                data=_serial_bundle(),
                content_type="multipart/form-data",
            )
    assert res.status_code == 500
    assert res.json["message"] == "An internal server error occurred."
    assert "Could not reach" not in res.get_data(as_text=True)


def test_validate_workflow_specification_unexpected_error_is_opaque(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """Unexpected validation exceptions are logged but not returned verbatim."""
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    with app.test_client() as client, patch(
        "reana_server.rest.workflows.validate_spec_bundle",
        side_effect=RuntimeError("/private/cluster/path"),
    ):
        res = client.post(
            url_for("workflows.validate_workflow_specification"),
            query_string={"access_token": user0.access_token},
            data=_serial_bundle(),
            content_type="multipart/form-data",
        )

    assert res.status_code == 500
    assert res.json["message"] == "An internal server error occurred."
    assert "/private/cluster/path" not in res.get_data(as_text=True)


def test_validate_workflow_specification_invalid_spec_returns_200(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """A genuinely invalid specification stays a 200 ``valid:false`` report.

    Only *service* failures are 500; a spec that loads but fails policy (or fails
    to load) is a normal validation outcome returned with HTTP 200 so the client
    can render the structured errors.
    """
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    invalid_report = {
        "valid": False,
        "reana_specification": None,
        "errors": [{"code": "load", "message": "bad spec", "path": ""}],
        "warnings": [],
    }
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.validate_spec_bundle",
            return_value=invalid_report,
        ):
            res = client.post(
                url_for("workflows.validate_workflow_specification"),
                query_string={"access_token": user0.access_token},
                data=_serial_bundle(),
                content_type="multipart/form-data",
            )
    assert res.status_code == 200
    assert res.json["valid"] is False
    assert res.json["errors"][0]["code"] == "load"
    assert "reana_specification" not in res.json


def test_validate_does_not_echo_large_expanded_specification(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """An expanded spec above the Go response cap remains an internal artifact."""
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    report = {
        "valid": True,
        "reana_specification": {"padding": "x" * (17 * 1024 * 1024)},
        "errors": [],
        "warnings": [],
    }
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.validate_spec_bundle", return_value=report
        ):
            res = client.post(
                url_for("workflows.validate_workflow_specification"),
                query_string={"access_token": user0.access_token},
                data=_serial_bundle(),
                content_type="multipart/form-data",
            )

    assert res.status_code == 200
    assert "reana_specification" not in res.json
    assert len(res.data) < 16 * 1024 * 1024


@pytest.mark.parametrize(
    "spec",
    [
        "workflow: [unterminated\n",
        "- workflow\n- is\n- a\n- list\n",
        "just-a-scalar\n",
        "workflow: []\n",
        "workflow: not-a-mapping\n",
    ],
)
def test_validate_malformed_or_non_mapping_yaml_returns_load_report(
    app, user0, _get_user_mock, monkeypatch, tmp_path, spec
):
    """Malformed/non-mapping YAML is an invalid spec, not an HTTP 500."""
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    with app.test_client() as client:
        res = client.post(
            url_for("workflows.validate_workflow_specification"),
            query_string={"access_token": user0.access_token},
            data=_bundle_with_spec(spec),
            content_type="multipart/form-data",
        )

    assert res.status_code == 200
    assert res.json["valid"] is False
    assert res.json["errors"][0]["code"] == "load"


@pytest.mark.parametrize(
    "spec",
    [
        "workflow: [unterminated\n",
        "[workflow, is, a, list]\n",
        "workflow: []\n",
    ],
)
def test_create_malformed_or_non_mapping_yaml_returns_400(
    app, user0, _get_user_mock, monkeypatch, tmp_path, spec
):
    """Create rejects malformed/non-mapping YAML without calling controller."""
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    rwc_client = Mock()
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client", rwc_client):
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={
                    "access_token": user0.access_token,
                    "workflow_name": "invalid-spec",
                },
                data=_bundle_with_spec(spec),
                content_type="multipart/form-data",
            )

    assert res.status_code == 400
    rwc_client.api.create_workflow.assert_not_called()


def _post_chunked_multipart(client, path, query_string, parts):
    """POST a multipart body with NO ``Content-Length`` (a chunked upload).

    Mirrors ``_serial_bundle()`` but forces a chunked transfer (no
    ``Content-Length``, ``wsgi.input_terminated``) so the up-front
    ``_validate_spec_bundle_request_size`` check is bypassed and only the
    per-view request-level cap (``request.max_content_length``) can reject the
    body -- the exact threat the cap defends against.

    :param parts: mapping of form-field name -> raw ``bytes`` payload.
    """
    boundary = "REANABUNDLEBOUNDARY"
    body = b""
    for name, data in parts.items():
        body += (
            ("--%s\r\n" % boundary).encode()
            + (
                'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (name, name)
            ).encode()
            + b"Content-Type: application/octet-stream\r\n\r\n"
            + data
            + b"\r\n"
        )
    body += ("--%s--\r\n" % boundary).encode()
    builder = EnvironBuilder(
        path=path,
        method="POST",
        query_string=query_string,
        content_type="multipart/form-data; boundary=%s" % boundary,
        input_stream=BytesIO(body),
    )
    environ = builder.get_environ()
    # Drop Content-Length and mark the input as terminated so Werkzeug reads it
    # as a chunked stream (no length known up front).
    environ.pop("CONTENT_LENGTH", None)
    environ["wsgi.input_terminated"] = True
    environ["HTTP_TRANSFER_ENCODING"] = "chunked"
    return client.open(environ_overrides=environ)


def test_validate_workflow_specification_in_limit_bundle(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """Regression: a normal in-limit serial bundle still validates (200)."""
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    with app.test_client() as client:
        res = client.post(
            url_for("workflows.validate_workflow_specification"),
            query_string={"access_token": user0.access_token},
            data=_serial_bundle(),
            content_type="multipart/form-data",
        )
        assert res.status_code == 200
        # Staging is cleaned up; nothing lingers under the shared volume.
        staging = os.path.join(str(tmp_path), "validation-tmp")
        assert not os.path.isdir(staging) or not os.listdir(staging)


def test_validate_http_cap_includes_zip_and_multipart_overhead(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """An extracted-content-limit bundle is not rejected for framing bytes."""
    from reana_server.rest import workflows as workflow_views

    extracted_bytes = len(SERIAL_REANA_YAML.encode())
    bundle = _serial_bundle()
    assert bundle["bundle"][0].getbuffer().nbytes > extracted_bytes
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    # This compatibility-only attribute makes the test fail against the old
    # implementation, which reused the extracted-byte cap for HTTP framing.
    monkeypatch.setattr(
        workflow_views,
        "REANA_SPEC_BUNDLE_MAX_BYTES",
        extracted_bytes,
        raising=False,
    )
    monkeypatch.setattr(
        "reana_server.specification_bundles.REANA_SPEC_BUNDLE_MAX_BYTES",
        extracted_bytes,
    )
    monkeypatch.setattr(
        "reana_server.rest.workflows.REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES",
        extracted_bytes + 2048,
    )
    with app.test_client() as client:
        res = client.post(
            url_for("workflows.validate_workflow_specification"),
            query_string={"access_token": user0.access_token},
            data=bundle,
            content_type="multipart/form-data",
        )
    assert res.status_code == 200


def test_validate_workflow_specification_chunked_over_cap_returns_413(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """A chunked over-cap bundle upload is rejected mid-parse with 413.

    A chunked upload carries no ``Content-Length``, so the up-front size check is
    bypassed. The per-view ``request.max_content_length`` cap must still abort
    multipart parsing before ``request.files`` is materialised, so nothing is
    staged under ``SHARED_VOLUME_PATH/validation-tmp/``. The cap is lowered so the
    test body stays tiny.
    """
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(
        "reana_server.rest.workflows.REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES", 100
    )
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows._stage_validation_bundle"
        ) as stage_mock:
            res = _post_chunked_multipart(
                client,
                url_for("workflows.validate_workflow_specification"),
                {"access_token": user0.access_token},
                {"bundle": b"X" * 500},
            )
    assert res.status_code == 413
    # Parsing aborted before request.files was read, so staging never ran.
    stage_mock.assert_not_called()
    staging = os.path.join(str(tmp_path), "validation-tmp")
    assert not os.path.isdir(staging) or not os.listdir(staging)


@pytest.mark.parametrize(
    ("endpoint", "query"),
    [
        ("workflows.validate_workflow_specification", {}),
        ("workflows.create_workflow", {"workflow_name": "oversized-spec"}),
    ],
)
def test_spec_bundle_content_length_over_cap_returns_413(
    app, user0, _get_user_mock, monkeypatch, endpoint, query
):
    """Known-length oversized bundles consistently return HTTP 413."""
    monkeypatch.setattr(
        "reana_server.rest.workflows.REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES", 100
    )
    query = {"access_token": user0.access_token, **query}
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows._stage_validation_bundle"
        ) as stage_mock:
            res = client.post(
                url_for(endpoint),
                query_string=query,
                data=_serial_bundle(),
                content_type="multipart/form-data",
            )
    assert res.status_code == 413
    assert "maximum is 100 bytes" in res.json["message"]
    stage_mock.assert_not_called()


def test_start_workflow_validates_specification(
    app, session, user0, sample_serial_workflow_in_db
):
    """Start re-validates the (authoritative) workspace, not the stored spec.

    The workspace is the source of truth and is mutable, so an invalid spec in
    the workspace must block the start even when the stored (DB) specification is
    still valid. A serial spec with a path-traversal input fails the in-process
    validator deterministically.
    """
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.created
    workflow.name = "test"
    session.add(workflow)
    session.commit()

    invalid_spec = (
        "workflow:\n"
        "  type: serial\n"
        "  specification:\n"
        "    steps:\n"
        "      - name: step1\n"
        "        environment: 'docker.io/library/busybox:1.36'\n"
        "        commands:\n"
        "          - echo hello\n"
        "inputs:\n"
        "  files:\n"
        "    - ../escape.txt\n"
    )
    with open(os.path.join(workflow.workspace_path, "reana.yaml"), "w") as f:
        f.write(invalid_spec)

    with app.test_client() as client:
        res = client.post(
            url_for(
                "workflows.start_workflow",
                workflow_id_or_name=str(workflow.id_),
            ),
            headers={"Content-Type": "application/json"},
            query_string={
                "access_token": user0.access_token,
            },
            data=json.dumps({}),
        )
        assert res.status_code == 400


def test_start_workflow_unexpected_error_is_opaque(
    app, session, user0, sample_serial_workflow_in_db
):
    """Unexpected start-time validation failures do not disclose internals."""
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.created
    workflow.name = "test"
    session.add(workflow)
    session.commit()
    with open(os.path.join(workflow.workspace_path, "reana.yaml"), "w") as f:
        f.write(SERIAL_REANA_YAML)

    with app.test_client() as client, patch(
        "reana_server.rest.workflows.load_and_validate_spec",
        side_effect=RuntimeError("private Kubernetes API detail"),
    ):
        res = client.post(
            url_for("workflows.start_workflow", workflow_id_or_name="test"),
            headers={"Content-Type": "application/json"},
            query_string={"access_token": user0.access_token},
            data=json.dumps({}),
        )

    assert res.status_code == 500
    assert res.json["message"] == "An internal server error occurred."
    assert "private Kubernetes API detail" not in res.get_data(as_text=True)


def test_start_workflow_succeeds_with_valid_workspace(
    app, session, user0, sample_serial_workflow_in_db
):
    """A valid workspace passes the binding gate and the workflow is queued."""
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.created
    workflow.name = "test"
    session.add(workflow)
    session.commit()

    with open(os.path.join(workflow.workspace_path, "reana.yaml"), "w") as f:
        f.write(SERIAL_REANA_YAML)

    with app.test_client() as client:
        with patch("reana_server.rest.workflows.publish_workflow_submission"):
            res = client.post(
                url_for(
                    "workflows.start_workflow",
                    workflow_id_or_name=str(workflow.id_),
                ),
                headers={"Content-Type": "application/json"},
                query_string={
                    "access_token": user0.access_token,
                },
                data=json.dumps({}),
            )
    assert res.status_code == 200
    assert res.json["status"] == RunStatus.queued.name


def test_start_workflow_falls_back_to_stored_spec_without_workspace_reana_yaml(
    app, session, user0, sample_serial_workflow_in_db
):
    """A workspace with no reana.yaml falls back to the stored spec (SNDBX-02).

    Launched workflows have their reana.yaml stripped by ``filter_input_files``
    and legacy (pre-seeding) workflows never had one. The binding gate must not
    hard-fail those: it validates the stored authoritative specification
    in-process instead of trying to re-load a non-existent workspace reana.yaml.
    """
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.created
    workflow.name = "test"
    # A known-valid stored specification (the workspace deliberately has none).
    workflow.reana_specification = yaml.safe_load(SERIAL_REANA_YAML)
    session.add(workflow)
    session.commit()

    # Ensure the workspace exists but carries no reana.yaml/reana.yml.
    os.makedirs(workflow.workspace_path, exist_ok=True)
    for name in ("reana.yaml", "reana.yml"):
        spec_path = os.path.join(workflow.workspace_path, name)
        if os.path.exists(spec_path):
            os.remove(spec_path)

    with app.test_client() as client:
        with patch("reana_server.rest.workflows.publish_workflow_submission"):
            res = client.post(
                url_for(
                    "workflows.start_workflow",
                    workflow_id_or_name=str(workflow.id_),
                ),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": user0.access_token},
                data=json.dumps({}),
            )
    assert res.status_code == 200
    assert res.json["status"] == RunStatus.queued.name


def test_start_endpoint_rejects_restart_replacement_payload(
    app, session, user0, sample_serial_workflow_in_db
):
    """A released client's replacement restart gets actionable upgrade guidance."""
    with app.test_client() as client:
        sample_serial_workflow_in_db.status = RunStatus.finished
        sample_serial_workflow_in_db.name = "test"
        session.add(sample_serial_workflow_in_db)
        session.commit()

        workflow_specification = copy.deepcopy(
            sample_serial_workflow_in_db.reana_specification
        )
        workflow_specification["workflow"]["type"] = "unknown"
        body = {
            "reana_specification": workflow_specification,
            "restart": True,
        }
        res = client.post(
            url_for("workflows.start_workflow", workflow_id_or_name="test"),
            headers={"Content-Type": "application/json"},
            query_string={
                "access_token": user0.access_token,
            },
            data=json.dumps(body),
        )
        assert res.status_code == 400
        assert "reana_specification" in res.json["message"]
        assert "upgrade REANA client" in res.json["message"]
        assert "/api/workflows/{id}/restart" in res.json["message"]


def test_atomic_restart_posts_replacement_and_parameters_together(
    app, session, user0, sample_serial_workflow_in_db
):
    """The multipart operation validates, promotes and submits one replacement."""
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.finished
    workflow.name = "test"
    session.add(workflow)
    session.commit()
    canonical_path = os.path.join(workflow.workspace_path, "reana.yaml")
    with open(canonical_path, "w") as stream:
        stream.write(SERIAL_REANA_YAML)
    replacement = SERIAL_REANA_YAML.replace("echo hello", "echo replacement")

    with app.test_client() as client, patch(
        "reana_server.rest.workflows.publish_workflow_submission"
    ) as publish_mock, patch(
        "reana_server.rest.workflows._recalculate_shared_workspace_quota"
    ):
        res = client.post(
            url_for("workflows.restart_workflow", workflow_id_or_name="test"),
            query_string={"access_token": user0.access_token},
            data={
                "replacement": (BytesIO(replacement.encode()), "replacement.yaml"),
                "parameters": json.dumps(
                    {
                        "input_parameters": {},
                        "operational_options": {"CACHE": "off"},
                    }
                ),
            },
        )

    assert res.status_code == 200
    assert publish_mock.call_args.args[2] == {
        "restart": True,
        "input_parameters": {},
        "operational_options": {"CACHE": "off"},
    }
    with open(canonical_path) as stream:
        assert stream.read() == replacement


def test_atomic_restart_failure_restores_canonical_specification(
    app, session, user0, sample_serial_workflow_in_db
):
    """A submission failure restores the previous bytes before releasing the lock."""
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.finished
    workflow.name = "test"
    workflow.reana_specification = copy.deepcopy(workflow.reana_specification)
    workflow.reana_specification["workspace"] = {"retention_days": {"**/*.txt": 1}}
    session.add(workflow)
    session.commit()
    workflow.set_workspace_retention_rules(
        [
            {"workspace_files": "active.txt", "retention_days": 1},
            {"workspace_files": "inactive.txt", "retention_days": 2},
            {"workspace_files": "applied.txt", "retention_days": 3},
        ]
    )
    original_rules = sorted(
        workflow.retention_rules, key=lambda rule: rule.workspace_files
    )
    original_rules[0].status = WorkspaceRetentionRuleStatus.active
    original_rules[1].status = WorkspaceRetentionRuleStatus.applied
    original_rules[2].status = WorkspaceRetentionRuleStatus.inactive
    session.commit()
    expected_original_states = {
        rule.id_: rule.status for rule in workflow.retention_rules
    }
    canonical_path = os.path.join(workflow.workspace_path, "reana.yaml")
    original = b"# original bytes\n" + SERIAL_REANA_YAML.encode()
    with open(canonical_path, "wb") as stream:
        stream.write(original)
    replacement = (
        SERIAL_REANA_YAML.replace("echo hello", "echo replacement")
        + "workspace:\n  retention_days:\n    '**/*.txt': 1\n"
    )

    with app.test_client() as client, patch(
        "reana_server.rest.workflows.publish_workflow_submission",
        side_effect=RuntimeError("broker unavailable"),
    ), patch("reana_server.rest.workflows._recalculate_shared_workspace_quota"):
        res = client.post(
            url_for("workflows.restart_workflow", workflow_id_or_name="test"),
            query_string={"access_token": user0.access_token},
            data={"replacement": (BytesIO(replacement.encode()), "replacement.yaml")},
        )

    assert res.status_code == 500
    session.expire_all()
    runs = (
        session.query(Workflow)
        .filter(Workflow.owner_id == user0.id_, Workflow.name == "test")
        .order_by(Workflow.run_number_minor)
        .all()
    )
    assert len(runs) == 2
    original_workflow, failed_clone = runs
    assert original_workflow.id_ == workflow.id_
    assert original_workflow.status == RunStatus.finished
    assert failed_clone.status == RunStatus.deleted
    assert {
        rule.id_: rule.status for rule in original_workflow.retention_rules
    } == expected_original_states
    assert failed_clone.retention_rules
    assert {rule.status for rule in failed_clone.retention_rules} == {
        WorkspaceRetentionRuleStatus.inactive
    }
    with open(canonical_path, "rb") as stream:
        assert stream.read() == original
    assert not any(
        name.startswith((".reana-promote-", ".reana-backup-"))
        for name in os.listdir(workflow.workspace_path)
    )
    # The shared database workflow fixture does not own retention rows; remove
    # the state created specifically for this regression before its teardown.
    session.query(WorkspaceRetentionAuditLog).delete()
    session.query(WorkspaceRetentionRule).filter(
        WorkspaceRetentionRule.workflow_id.in_(
            [original_workflow.id_, failed_clone.id_]
        )
    ).delete(synchronize_session=False)
    session.commit()
    session.expire(original_workflow, ["retention_rules"])


def test_atomic_restart_rejects_malformed_parameters_before_cloning(
    app, session, user0, sample_serial_workflow_in_db
):
    """Multipart parameters have one small JSON-object contract."""
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.finished
    workflow.name = "test"
    session.add(workflow)
    session.commit()

    with app.test_client() as client, patch(
        "reana_server.rest.workflows.clone_workflow"
    ) as clone_workflow:
        res = client.post(
            url_for("workflows.restart_workflow", workflow_id_or_name="test"),
            query_string={"access_token": user0.access_token},
            data={
                "replacement": (BytesIO(SERIAL_REANA_YAML.encode()), "reana.yaml"),
                "parameters": json.dumps({"restart": True}),
            },
        )

    assert res.status_code == 400
    assert "Unknown restart parameters" in res.json["message"]
    clone_workflow.assert_not_called()


def test_atomic_restart_rejects_missing_workspace_source(
    app, session, user0, sample_serial_workflow_in_db
):
    """Replacement specs may only reference source already in the workspace."""
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.finished
    workflow.name = "test"
    workflow.reana_specification = {
        "workflow": {
            "type": "snakemake",
            "file": "workflow/missing: Snakefile",
        },
        "inputs": {"parameters": {}},
    }
    session.add(workflow)
    session.commit()
    replacement = yaml.safe_dump(workflow.reana_specification)

    with app.test_client() as client, patch(
        "reana_server.rest.workflows.clone_workflow"
    ) as clone_workflow:
        res = client.post(
            url_for("workflows.restart_workflow", workflow_id_or_name="test"),
            query_string={"access_token": user0.access_token},
            data={"replacement": (BytesIO(replacement.encode()), "replacement.yaml")},
        )

    assert res.status_code == 400
    assert res.json["message"] == (
        "Workflow source path 'workflow/missing: Snakefile' referenced by the "
        "restart specification is not present in the workspace."
    )
    clone_workflow.assert_not_called()


def test_restart_overlong_path_error_is_bounded():
    """An invalid declaration cannot be reflected into a huge API response."""
    from reana_server.rest.workflows import _restart_specification_path_error

    error = REANASpecificationPathError(
        "overlong",
        "workflow.file",
        "x" * (17 * 1024 * 1024),
        "max_length",
    )

    message = str(_restart_specification_path_error(error))
    assert (
        message == "Workflow source path in workflow.file exceeds 4096 encoded bytes."
    )
    assert len(message) < 100


def test_atomic_restart_formats_unsafe_workspace_source(
    app, session, user0, sample_serial_workflow_in_db
):
    """Typed unsafe paths receive stable restart-specific diagnostics."""
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.finished
    workflow.name = "test"
    session.commit()
    replacement = yaml.safe_dump(
        {
            "workflow": {"type": "snakemake", "file": "../Snakefile"},
            "inputs": {"parameters": {}},
        }
    )

    with app.test_client() as client, patch(
        "reana_server.rest.workflows.clone_workflow"
    ) as clone_workflow:
        res = client.post(
            url_for("workflows.restart_workflow", workflow_id_or_name="test"),
            query_string={"access_token": user0.access_token},
            data={"replacement": (BytesIO(replacement.encode()), "replacement.yaml")},
        )

    assert res.status_code == 400
    assert res.json["message"] == (
        "Workflow source path '../Snakefile' referenced by the restart specification "
        "is unsafe."
    )
    clone_workflow.assert_not_called()


def test_enforce_restart_spec_constraints_allows_present_changed_file(tmp_path):
    """A restart may change ``workflow.file`` when the new source is present.

    The narrowed restart contract only requires that every referenced
    workflow-source file exists in the workspace; a user who uploaded a new
    source file may point ``workflow.file`` at it, even though it differs from
    the original run.
    """
    from reana_server.rest.workflows import _enforce_restart_spec_constraints

    os.mkdir(os.path.join(str(tmp_path), "workflow"))
    with open(os.path.join(str(tmp_path), "workflow", "Snakefile2"), "w") as f:
        f.write("rule all:\n    input: []\n")

    workflow = Mock()
    workflow.workspace_path = str(tmp_path)
    workflow.reana_specification = {
        "workflow": {"type": "snakemake", "file": "workflow/Snakefile"},
    }
    new_spec = {"workflow": {"type": "snakemake", "file": "workflow/Snakefile2"}}

    # Must not raise: workflow.file changed, but the new source is present.
    _enforce_restart_spec_constraints(workflow, new_spec)


def test_info_surfaces_kubernetes_min_user_uid(app, user0, _get_user_mock):
    """Test /info exposes the configured minimum Kubernetes user ID."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.info.REANA_KUBERNETES_JOBS_MIN_USER_UID", 1234
        ), patch(
            "reana_server.rest.info.REANA_INTERACTIVE_SESSIONS_ENVIRONMENTS",
            {"jupyter": {"recommended": []}},
        ):
            res = client.get(
                url_for("info.info"),
                query_string={"access_token": user0.access_token},
            )
    assert res.status_code == 200
    payload = res.json
    assert "kubernetes_min_user_uid" in payload
    assert payload["kubernetes_min_user_uid"]["value"] == 1234
    assert (
        payload["kubernetes_min_user_uid"]["title"]
        == "Minimum allowed user runtime container UID for Kubernetes jobs"
    )


def test_patch_quota_rejects_invalid_json_body(app):
    """Test PATCH /api/quota returns a JSON 400 for invalid JSON bodies."""
    with app.test_client() as client:
        with patch("reana_server.rest.quota.REANA_QUOTA_MANAGEMENT_SECRET", "secret"):
            res = client.patch(
                url_for("quota.patch_quota"),
                headers={
                    "Content-Type": "application/json",
                    "X-Quota-Management-Secret": "secret",
                },
                data="not-json",
            )

    assert res.status_code == 400
    assert res.json["message"] == "Invalid request. Expected application/json body."


def test_patch_quota_rejects_non_integer_quota_period_months(app):
    """Test PATCH /api/quota rejects non-integer quota period month values."""
    with app.test_client() as client:
        with patch("reana_server.rest.quota.REANA_QUOTA_MANAGEMENT_SECRET", "secret"):
            res = client.patch(
                url_for("quota.patch_quota"),
                headers={
                    "Content-Type": "application/json",
                    "X-Quota-Management-Secret": "secret",
                },
                data=json.dumps(
                    {
                        "email": "user@example.org",
                        "resource_type": "cpu",
                        "quota_period_months": 0.2,
                    }
                ),
            )

    assert res.status_code == 400
    assert (
        res.json["message"]
        == "Invalid request. Errors: {'quota_period_months': ['Not a valid integer.']}"
    )


def test_get_workflow_specification(
    app, user0, _get_user_mock, sample_yadage_workflow_in_db
):
    """Test get_workflow_specification view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(
                url_for("workflows.get_workflow_specification", workflow_id_or_name="1")
            )
            assert res.status_code == 401

            res = client.get(
                url_for(
                    "workflows.get_workflow_specification", workflow_id_or_name="1"
                ),
                query_string={"access_token": "wrongtoken"},
            )
            assert res.status_code == 403

            res = client.get(
                url_for(
                    "workflows.get_workflow_specification",
                    workflow_id_or_name=sample_yadage_workflow_in_db.id_,
                ),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": user0.access_token},
                data=json.dumps(None),
            )
            parsed_res = json.loads(res.data)
            assert res.status_code == 200
            specification = parsed_res["specification"]
            assert (
                specification["workflow"]["specification"]
                == sample_yadage_workflow_in_db.get_specification()
            )
            assert (
                specification["inputs"]["parameters"]
                == sample_yadage_workflow_in_db.get_input_parameters()
            )
            assert (
                specification["workflow"]["type"] == sample_yadage_workflow_in_db.type_
            )


def test_get_workflow_logs(app, user0, _get_user_mock):
    """Test get_workflow_logs view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(
                url_for("workflows.get_workflow_logs", workflow_id_or_name="1")
            )
            assert res.status_code == 401

            res = client.get(
                url_for("workflows.get_workflow_logs", workflow_id_or_name="1"),
                query_string={"access_token": "wrongtoken"},
            )
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.get_workflow_logs", workflow_id_or_name="1"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": user0.access_token},
                data=json.dumps(None),
            )
            assert res.status_code == 200


def test_get_workflow_disk_usage_hides_htcondor_transfer_paths(
    app,
    session,
    user1,
    user2,
    sample_serial_workflow_in_db_owned_by_user1,
):
    """Test hiding internal HTCondor transfer paths from disk usage details."""
    workflow = sample_serial_workflow_in_db_owned_by_user1
    session.add(UserWorkflow(workflow_id=workflow.id_, user_id=user2.id_))
    session.commit()

    detailed_usage = [
        {"name": "", "size": {"raw": 42}},
        {"name": "/result.txt", "size": {"raw": 10}},
        {
            "name": "/reana_job.123.filetransfer",
            "size": {"raw": 20},
        },
        {
            "name": "/reana_job.123.filetransfer/credential.cc",
            "size": {"raw": 10},
        },
        {
            "name": "/output/reana_job.123.filetransfer/user-result.txt",
            "size": {"raw": 2},
        },
    ]
    summary_usage = [{"name": "", "size": {"raw": 42}}]
    requested_modes = []

    def get_workspace_disk_usage(_workflow, summarize=False, search=None):
        requested_modes.append((summarize, search))
        return summary_usage if summarize else detailed_usage

    with patch.object(
        Workflow,
        "get_workspace_disk_usage",
        autospec=True,
        side_effect=get_workspace_disk_usage,
    ), app.test_client() as client:
        for user in (user1, user2):
            response = client.get(
                url_for(
                    "workflows.get_workflow_disk_usage",
                    workflow_id_or_name=workflow.id_,
                ),
                query_string={"access_token": user.access_token},
                json={"summarize": False},
            )
            assert response.status_code == 200
            assert response.get_json()["disk_usage_info"] == [
                {"name": "", "size": {"raw": 42}},
                {"name": "/result.txt", "size": {"raw": 10}},
                {
                    "name": "/output/reana_job.123.filetransfer/user-result.txt",
                    "size": {"raw": 2},
                },
            ]

            response = client.get(
                url_for(
                    "workflows.get_workflow_disk_usage",
                    workflow_id_or_name=workflow.id_,
                ),
                query_string={"access_token": user.access_token},
                json={"summarize": True},
            )
            assert response.status_code == 200
            assert response.get_json()["disk_usage_info"] == summary_usage

    assert requested_modes == [
        (False, None),
        (True, None),
        (False, None),
        (True, None),
    ]


def test_summarized_disk_usage_search_hides_htcondor_transfer_paths(
    app,
    session,
    user2,
    sample_serial_workflow_in_db_owned_by_user1,
):
    """Test filtering real summarised disk usage results selected by name."""
    workflow = sample_serial_workflow_in_db_owned_by_user1
    session.add(UserWorkflow(workflow_id=workflow.id_, user_id=user2.id_))
    session.commit()

    visible_path = os.path.join(workflow.workspace_path, "result.txt")
    internal_path = os.path.join(
        workflow.workspace_path,
        "reana_job.123.filetransfer",
        "credential.cc",
    )
    os.makedirs(os.path.dirname(internal_path))
    with open(visible_path, "w") as visible_file:
        visible_file.write("visible result")
    with open(internal_path, "w") as credential_cache:
        credential_cache.write("kerberos credential")

    search = json.dumps(
        {
            "name": [
                "result.txt",
                "reana_job.*.filetransfer/credential.cc",
            ]
        }
    )
    with app.test_client() as client:
        response = client.get(
            url_for(
                "workflows.get_workflow_disk_usage",
                workflow_id_or_name=workflow.id_,
            ),
            query_string={"access_token": user2.access_token},
            json={"summarize": True, "search": search},
        )

    assert response.status_code == 200
    disk_usage_names = {
        file_["name"] for file_ in response.get_json()["disk_usage_info"]
    }
    assert disk_usage_names == {"/result.txt"}


def test_get_workflow_status(app, user0, _get_user_mock):
    """Test get_workflow_logs view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(
                url_for("workflows.get_workflow_status", workflow_id_or_name="1"),
            )
            assert res.status_code == 401
            res = client.get(
                url_for("workflows.get_workflow_status", workflow_id_or_name="1"),
                query_string={"access_token": "wrongtoken"},
            )
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.get_workflow_status", workflow_id_or_name="1"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": user0.access_token},
            )
            assert res.status_code == 200


def test_set_workflow_status(app, user0, _get_user_mock):
    """Test set_workflow_status view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.put(
                url_for("workflows.set_workflow_status", workflow_id_or_name="1")
            )
            assert res.status_code == 401

            res = client.put(
                url_for("workflows.set_workflow_status", workflow_id_or_name="1"),
                query_string={"access_token": "wrongtoken"},
            )
            assert res.status_code == 403

            res = client.put(
                url_for("workflows.set_workflow_status", workflow_id_or_name="1"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": user0.access_token},
            )
            assert res.status_code == 422

            res = client.put(
                url_for("workflows.set_workflow_status", workflow_id_or_name="1"),
                headers={"Content-Type": "application/json"},
                query_string={
                    "access_token": user0.access_token,
                    "status": "stop",
                },
                data=json.dumps({}),
            )
            assert res.status_code == 200


def test_set_workflow_status_start_uses_submission_boundary(app, user0, _get_user_mock):
    """Legacy ``status=start`` uses the same serialized submission boundary."""
    rwc_mock = Mock()
    submission_response = {
        "message": "Workflow submitted.",
        "workflow_id": "1",
        "workflow_name": "test",
        "status": "queued",
        "run_number": 1,
        "user": str(user0.id_),
        "validation_warnings": [{"code": "tag_latest", "message": "latest"}],
    }
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client", rwc_mock
        ), patch(
            "reana_server.rest.workflows._submit_workflow",
            return_value=(submission_response, 200),
        ) as submit_workflow:
            res = client.put(
                url_for("workflows.set_workflow_status", workflow_id_or_name="1"),
                headers={"Content-Type": "application/json"},
                query_string={
                    "access_token": user0.access_token,
                    "status": "start",
                },
                data=json.dumps({"input_parameters": {"number": 42}}),
            )
    assert res.status_code == 200
    assert "run_number" not in res.json
    assert res.json["validation_warnings"] == [
        {"code": "tag_latest", "message": "latest"}
    ]
    submit_workflow.assert_called_once()
    args, kwargs = submit_workflow.call_args
    assert args[0] == "1"
    assert args[1].id_ == user0.id_
    assert kwargs == {"input_parameters": {"number": 42}}
    rwc_mock.api.set_workflow_status.assert_not_called()


def test_submission_boundary_enforces_quota(user0):
    """Every start surface enforces quota at the shared boundary."""
    from reana_server.rest.workflows import _submit_workflow

    with patch.object(user0, "has_exceeded_quota", return_value=True), patch(
        "reana_server.rest.workflows.get_quota_excess_message",
        return_value="Quota exceeded.",
    ):
        response, status_code = _submit_workflow("1", user0)

    assert status_code == 403
    assert response == {"message": "Quota exceeded."}


def test_start_workflow_returns_409_when_workspace_locked(
    app, session, user0, sample_serial_workflow_in_db
):
    """The ``/start`` route is serialized under the workspace-mutation lock.

    When another request already holds the workspace lock, ``/start`` returns a
    409 instead of racing a concurrent workspace mutation (e.g. an upload).
    """
    from reana_server.rest.workflows import WorkspaceMutationConflict

    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.created
    workflow.name = "test"
    session.add(workflow)
    session.commit()
    with open(os.path.join(workflow.workspace_path, "reana.yaml"), "w") as f:
        f.write(SERIAL_REANA_YAML)

    def _already_locked(*args, **kwargs):
        raise WorkspaceMutationConflict()

    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows._workspace_mutation_lock", _already_locked
        ):
            res = client.post(
                url_for(
                    "workflows.start_workflow",
                    workflow_id_or_name=str(workflow.id_),
                ),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": user0.access_token},
                data=json.dumps({}),
            )
    assert res.status_code == 409


def test_workspace_mutation_lock_releases_after_failure(app, session, tmp_path):
    """A failed mutation cannot leave its workspace advisory lock behind."""
    from reana_server.rest.workflows import (
        WorkspaceMutationConflict,
        _workspace_mutation_lock,
    )

    workspace_path = str(tmp_path / "workspace")

    with _workspace_mutation_lock(workspace_path):
        with pytest.raises(WorkspaceMutationConflict):
            with _workspace_mutation_lock(workspace_path):
                pass

    with pytest.raises(RuntimeError, match="mutation failed"):
        with _workspace_mutation_lock(workspace_path):
            raise RuntimeError("mutation failed")

    # PostgreSQL releases a transaction-scoped advisory lock on rollback, so
    # the same workspace must be lockable immediately after the failed request.
    with _workspace_mutation_lock(workspace_path):
        pass


@pytest.mark.parametrize(
    "error,status_code",
    [
        ("WorkspaceMutationConflict", 409),
        ("WorkspaceMutationUnavailable", 503),
    ],
)
def test_workspace_mutation_decorator_maps_lock_failures(
    app, user0, error, status_code
):
    """Workspace endpoints expose contention and infrastructure failures."""
    from reana_server.rest import workflows

    protected = workflows._serialize_workspace_mutation(lambda *_args, **_kwargs: None)
    workflow = Mock(workspace_path="/tmp/workspace")
    with app.test_request_context(), patch.object(
        workflows, "_get_workflow_with_uuid_or_name", return_value=workflow
    ), patch.object(
        workflows, "_workspace_mutation_lock", side_effect=getattr(workflows, error)()
    ):
        response, actual_status = protected("workflow", user=user0)

    assert actual_status == status_code
    assert response.get_json()["message"]


def test_status_delete_locks_all_workspaces_but_stop_does_not(app, user0):
    """Only destructive status changes enter the multi-workspace boundary."""
    from reana_server.rest import workflows

    acquired = []
    families = []

    @contextmanager
    def capture(paths):
        acquired.append(list(paths))
        yield

    @contextmanager
    def capture_family(owner_id, workflow_name):
        families.append((owner_id, workflow_name))
        yield

    http_response = Mock(status_code=200)
    api_client = Mock()
    api_client.api.set_workflow_status.return_value.result.return_value = (
        {"message": "ok"},
        http_response,
    )
    workflow = Mock(owner_id=user0.id_)
    workflow.name = "workflow"
    with app.test_client() as client, patch.object(
        workflows, "current_rwc_api_client", api_client
    ), patch.object(
        workflows, "_get_workflow_with_uuid_or_name", return_value=workflow
    ), patch.object(
        workflows,
        "_deletion_workspace_paths",
        return_value=["/workspace/b", "/workspace/a", "/workspace/a"],
    ), patch.object(
        workflows, "workspace_mutation_locks", capture
    ), patch.object(
        workflows, "workflow_family_mutation_lock", capture_family
    ):
        deleted = client.put(
            url_for("workflows.set_workflow_status", workflow_id_or_name="workflow"),
            query_string={"access_token": user0.access_token, "status": "deleted"},
            json={"all_runs": True},
        )
        stopped = client.put(
            url_for("workflows.set_workflow_status", workflow_id_or_name="workflow"),
            query_string={"access_token": user0.access_token, "status": "stop"},
            json={},
        )

    assert deleted.status_code == 200
    assert stopped.status_code == 200
    assert acquired == [["/workspace/b", "/workspace/a", "/workspace/a"]]
    assert families == [(user0.id_, "workflow")]


def test_shared_workspace_quota_scans_once_for_all_siblings(
    session, user0, sample_serial_workflow_in_db
):
    """Restart quota reconciliation measures one shared filesystem tree once."""
    from reana_server.rest.workflows import _recalculate_shared_workspace_quota

    workflow = sample_serial_workflow_in_db
    sibling = Workflow(
        id_=uuid4(),
        name=workflow.name,
        owner_id=user0.id_,
        reana_specification=workflow.reana_specification,
        type_=workflow.type_,
        workspace_path=workflow.workspace_path,
    )
    session.add(sibling)
    session.commit()

    with patch(
        "reana_server.rest.workflows.get_disk_usage_or_zero", return_value=123
    ) as disk_usage, patch(
        "reana_server.rest.workflows.update_users_disk_quota"
    ) as update_user:
        _recalculate_shared_workspace_quota(workflow, user0)

    disk_usage.assert_called_once_with(
        workflow.workspace_path, override_policy_checks=True
    )
    update_user.assert_called_once_with(user0, override_policy_checks=True)
    resources = (
        session.query(WorkflowResource)
        .filter(WorkflowResource.workflow_id.in_([workflow.id_, sibling.id_]))
        .all()
    )
    assert len(resources) == 2
    assert {resource.quota_used for resource in resources} == {123}


def test_upload_file(app, user0, _get_user_mock):
    """Test upload_file view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.requests"):
            file_content = b"Upload this data."
            res = client.post(
                url_for("workflows.upload_file", workflow_id_or_name="1"),
                query_string={"file_name": "test_upload.txt"},
                input_stream=BytesIO(file_content),
            )
            assert res.status_code == 401

            res = client.post(
                url_for("workflows.upload_file", workflow_id_or_name="1"),
                query_string={
                    "file_name": "test_upload.txt",
                    "access_token": "wrongtoken",
                },
                input_stream=BytesIO(file_content),
            )
            assert res.status_code == 403

            # wrong content type
            res = client.post(
                url_for("workflows.upload_file", workflow_id_or_name="1"),
                query_string={
                    "access_token": user0.access_token,
                    "file_name": "test_upload.txt",
                },
                headers={"Content-Type": "multipart/form-data"},
                input_stream=BytesIO(file_content),
            )
            assert res.status_code == 400
            # missing file name
            res = client.post(
                url_for("workflows.upload_file", workflow_id_or_name="1"),
                query_string={
                    "access_token": user0.access_token,
                    "file_name": None,
                },
                headers={"Content-Type": "application/octet-stream"},
                input_stream=BytesIO(file_content),
            )
            assert res.status_code == 400

        requests_mock = Mock()
        requests_response_mock = Mock()
        requests_response_mock.status_code = 200
        requests_response_mock.json = Mock(return_value={"message": "File uploaded."})
        forwarded_contents = []
        forwarded_lengths = []

        def _capture_forwarded_upload(*args, **kwargs):
            forwarded_lengths.append(len(kwargs["data"]))
            forwarded_contents.append(kwargs["data"].read())
            return requests_response_mock

        requests_mock.post = Mock(side_effect=_capture_forwarded_upload)
        with patch(
            "reana_server.rest.workflows.requests", requests_mock
        ) as requests_client, patch(
            "reana_server.rest.workflows.prevent_disk_quota_excess"
        ) as quota_check:
            res = client.post(
                url_for("workflows.upload_file", workflow_id_or_name="1"),
                query_string={
                    "access_token": user0.access_token,
                    "file_name": "test_upload.txt",
                },
                headers={"Content-Type": "application/octet-stream"},
                input_stream=BytesIO(file_content),
            )
            requests_client.post.assert_called_once()
            assert forwarded_lengths[0] == len(file_content)
            assert forwarded_contents[0] == file_content

            # Multipart is intentionally not accepted for potentially large
            # workspace data because Werkzeug would spool it before quota.
            res = client.post(
                url_for("workflows.upload_file", workflow_id_or_name="1"),
                query_string={
                    "access_token": user0.access_token,
                    "file_name": "multipart-upload.txt",
                },
                data={"file": (BytesIO(file_content), "local-file.txt")},
                content_type="multipart/form-data",
            )
            assert res.status_code == 400
            assert requests_client.post.call_count == 1

            # empty file
            res = client.post(
                url_for("workflows.upload_file", workflow_id_or_name="1"),
                query_string={
                    "access_token": user0.access_token,
                    "file_name": "empty.txt",
                },
                headers={"Content-Type": "application/octet-stream"},
                input_stream=BytesIO(b""),
            )
            assert requests_client.post.call_count == 2
            data = requests_client.post.call_args_list[1][1]["data"]
            assert not len(data)
            assert not forwarded_lengths[1]
            assert not forwarded_contents[1]

            assert [call.args[1] for call in quota_check.call_args_list] == [
                len(file_content),
                0,
            ]
            assert requests_client.post.call_args_list[0][1]["timeout"] == (10.0, 300.0)


def test_upload_controller_timeout_returns_503_and_releases_lock(
    app, user0, _get_user_mock
):
    """A stalled controller never strands the workspace mutation lock."""
    from reana_server.rest import workflows

    entered = []

    @contextmanager
    def lock(_path):
        entered.append("enter")
        try:
            yield
        finally:
            entered.append("exit")

    with app.test_client() as client, patch.object(
        workflows, "_workspace_mutation_lock", lock
    ), patch.object(
        workflows.requests,
        "post",
        side_effect=workflows.requests.exceptions.Timeout(),
    ):
        response = client.post(
            url_for("workflows.upload_file", workflow_id_or_name="1"),
            query_string={
                "access_token": user0.access_token,
                "file_name": "input.dat",
            },
            headers={"Content-Type": "application/octet-stream"},
            input_stream=BytesIO(b"payload"),
        )

    assert response.status_code == 503
    assert entered == ["enter", "exit"]


def test_download_file(app, user0, _get_user_mock):
    """Test download_file view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.requests"):
            res = client.get(
                url_for(
                    "workflows.download_file",
                    workflow_id_or_name="1",
                    file_name="test_download",
                ),
                query_string={
                    "file_name": "test_upload.txt",
                },
            )
            assert res.status_code == 401

        with patch("reana_server.rest.workflows.requests"):
            res = client.get(
                url_for(
                    "workflows.download_file",
                    workflow_id_or_name="1",
                    file_name="test_download",
                ),
                query_string={
                    "file_name": "test_upload.txt",
                    "access_token": "wrongtoken",
                },
            )
            assert res.status_code == 403

        requests_mock = Mock()
        requests_response_mock = Mock()
        requests_response_mock.status_code = 200
        requests_response_mock.json = Mock(return_value={"message": "File downloaded."})
        requests_mock.get = Mock(return_value=requests_response_mock)
        with patch(
            "reana_server.rest.workflows.requests", requests_mock
        ) as requests_client:
            res = client.get(
                url_for(
                    "workflows.download_file",
                    workflow_id_or_name="1",
                    file_name="test_download",
                ),
                query_string={"access_token": user0.access_token},
            )

            requests_client.get.assert_called_once()
            assert requests_client.get.return_value.status_code == 200


def test_delete_file(app, user0, _get_user_mock):
    """Test delete_file view."""
    mock_response = Mock()
    mock_response.headers = {"Content-Type": "multipart/form-data"}
    mock_response.json = Mock(return_value={})
    mock_response.status_code = 200
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(
                mock_http_response=mock_response
            ),
        ):
            res = client.delete(
                url_for(
                    "workflows.delete_file",
                    workflow_id_or_name="1",
                    file_name="test_delete.txt",
                )
            )
            assert res.status_code == 401

            res = client.delete(
                url_for(
                    "workflows.delete_file",
                    workflow_id_or_name="1",
                    file_name="test_delete.txt",
                ),
                query_string={
                    "access_token": "wrongtoken",
                },
            )
            assert res.status_code == 403

            res = client.delete(
                url_for(
                    "workflows.delete_file",
                    workflow_id_or_name="1",
                    file_name="test_delete.txt",
                ),
                query_string={"access_token": user0.access_token},
            )
            assert res.status_code == 200


def test_get_files(app, user0, _get_user_mock):
    """Test get_files view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(url_for("workflows.get_files", workflow_id_or_name="1"))
            assert res.status_code == 401

            res = client.get(
                url_for("workflows.get_files", workflow_id_or_name="1"),
                query_string={"access_token": "wrongtoken"},
            )
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.get_files", workflow_id_or_name="1"),
                query_string={"access_token": user0.access_token},
            )
            assert res.status_code == 500

        mock_http_response = Mock()
        mock_http_response.status_code = 200
        mock_http_response.json.return_value = dict(key="value")
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(
                mock_http_response=mock_http_response
            ),
        ):
            res = client.get(
                url_for("workflows.get_files", workflow_id_or_name="1"),
                query_string={"access_token": user0.access_token},
            )
            assert res.status_code == 200


def test_move_files(app, user0, _get_user_mock):
    """Test move_files view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.put(
                url_for("workflows.move_files", workflow_id_or_name="1"),
                query_string={
                    "user": user0.id_,
                    "source": "source.txt",
                    "target": "target.txt",
                },
            )
            assert res.status_code == 401

            res = client.put(
                url_for("workflows.move_files", workflow_id_or_name="1"),
                query_string={
                    "user": user0.id_,
                    "source": "source.txt",
                    "target": "target.txt",
                    "access_token": "wrongtoken",
                },
            )
            assert res.status_code == 403

            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = dict(key="value")
            with patch(
                "reana_server.rest.workflows.current_rwc_api_client",
                make_mock_api_client("reana-workflow-controller")(
                    mock_http_response=mock_response
                ),
            ):
                res = client.put(
                    url_for("workflows.move_files", workflow_id_or_name="1"),
                    query_string={
                        "access_token": user0.access_token,
                        "source": "source.txt",
                        "target": "target.txt",
                    },
                )
                assert res.status_code == 200


@pytest.mark.parametrize(
    ("interactive_session_type", "expected_status_code"),
    [(int_session_type.name, 200) for int_session_type in InteractiveSessionType]
    + [("wrong-interactive-type", 404)],
)
def test_open_interactive_session(
    app,
    user0,
    sample_serial_workflow_in_db,
    interactive_session_type,
    expected_status_code,
    _get_user_mock,
):
    """Test open interactive session."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.post(
                url_for(
                    "workflows.open_interactive_session",
                    workflow_id_or_name=sample_serial_workflow_in_db.id_,
                    interactive_session_type=interactive_session_type,
                ),
                query_string={"access_token": user0.access_token},
            )
            assert res.status_code == expected_status_code


@pytest.mark.parametrize(("expected_status_code"), [200])
def test_close_interactive_session(
    app,
    user0,
    sample_serial_workflow_in_db,
    expected_status_code,
    _get_user_mock,
):
    """Test close an interactive session."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.post(
                url_for(
                    "workflows.close_interactive_session",
                    workflow_id_or_name=sample_serial_workflow_in_db.id_,
                ),
                query_string={"access_token": user0.access_token},
            )
            assert res.status_code == expected_status_code


def test_create_and_associate_oauth_user(app, session):
    user_email = "johndoe@reana.io"
    user_fullname = "John Doe"
    username = "johndoe"
    account_info = {
        "user": {
            "email": user_email,
            "profile": {"full_name": user_fullname, "username": username},
        }
    }
    user = session.query(User).filter_by(email=user_email).one_or_none()
    assert user is None
    _create_and_associate_oauth_user(None, account_info=account_info)
    user = session.query(User).filter_by(email=user_email).one_or_none()
    assert user
    assert user.email == user_email
    assert user.full_name == user_fullname
    assert user.username == username


def test_create_and_associate_local_user(app, session):
    mock_user = Mock(email="johndoe@reana.io")
    user = session.query(User).filter_by(email=mock_user.email).one_or_none()
    assert user is None
    with patch(
        "reana_server.utils._send_confirmation_email"
    ) as send_confirmation_email:
        _create_and_associate_local_user(None, user=mock_user)
        send_confirmation_email.assert_called_once()
    user = session.query(User).filter_by(email=mock_user.email).one_or_none()
    assert user
    assert user.email == mock_user.email
    assert user.full_name == mock_user.email
    assert user.username == mock_user.email


def test_get_workflow_retention_rules(app, user0):
    """Test get_workflow_retention_rules."""
    endpoint_url = url_for(
        "workflows.get_workflow_retention_rules", workflow_id_or_name="workflow"
    )
    with app.test_client() as client:
        # Token not provided
        res = client.get(endpoint_url)
        assert res.status_code == 401

        # Token not valid
        res = client.get(endpoint_url, query_string={"access_token": "invalid_token"})
        assert res.status_code == 403

        # Test that status code is propagated from r-w-controller
        status_code = 404
        mock_response = {"message": "error"}
        mock_http_response = Mock(status_code=status_code)
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(
                mock_response, mock_http_response
            ),
        ):
            res = client.get(
                endpoint_url, query_string={"access_token": user0.access_token}
            )
            assert res.status_code == status_code
            assert "message" in res.json


def test_prune_workspace(app, user0, sample_serial_workflow_in_db):
    """Test prune_workspace."""
    endpoint_url = url_for(
        "workflows.prune_workspace",
        workflow_id_or_name=sample_serial_workflow_in_db.id_,
    )
    with app.test_client() as client:
        # Test token not provided
        res = client.post(endpoint_url)
        assert res.status_code == 401

        # Test invalid token
        res = client.post(endpoint_url, query_string={"access_token": "invalid_token"})
        assert res.status_code == 403

        # Test invalid workflow name
        res = client.post(
            url_for(
                "workflows.prune_workspace",
                workflow_id_or_name="invalid_wf",
            ),
            query_string={"access_token": user0.access_token},
        )
        assert res.status_code == 403

        # Test normal behaviour
        status_code = 200
        res = client.post(
            endpoint_url, query_string={"access_token": user0.access_token}
        )
        assert res.status_code == status_code
        assert "The workspace has been correctly pruned." in res.json["message"]

        res = client.post(
            endpoint_url,
            query_string={
                "access_token": user0.access_token,
                "include_inputs": True,
                "include_outputs": True,
            },
        )
        assert res.status_code == status_code
        assert "The workspace has been correctly pruned." in res.json["message"]


def test_gitlab_projects(app: Flask, user0):
    """Test fetching of GitLab projects."""
    with app.test_client() as client:
        # token not provided
        res = client.get("/api/gitlab/projects")
        assert res.status_code == 401

        # invalid REANA token
        res = client.get(
            "/api/gitlab/projects", query_string={"access_token": "invalid"}
        )
        assert res.status_code == 403

        # missing GitLab token
        fetch_mock = Mock()
        fetch_mock.return_value = UserSecrets(
            user_id=str(user0.id_),
            k8s_secret_name="k8s_secret_name",
        )
        with patch(
            "reana_commons.k8s.secrets.UserSecretsStore.fetch",
            fetch_mock,
        ):
            res = client.get(
                "/api/gitlab/projects",
                query_string={"access_token": user0.access_token},
            )
            assert res.status_code == 401

        # normal behaviour
        mock_response_projects = Mock()
        mock_response_projects.headers = {
            "x-prev-page": "3",
            "x-next-page": "",
            "x-page": "4",
            "x-total": "100",
            "x-per-page": "20",
        }
        mock_response_projects.ok = True
        mock_response_projects.status_code = 200
        mock_response_projects.json.return_value = [
            {
                "id": 123,
                "path_with_namespace": "abcd",
                "web_url": "url",
                "name": "qwerty",
            }
        ]

        mock_response_webhook = Mock()
        mock_response_webhook.ok = True
        mock_response_webhook.status_code = 200
        mock_response_webhook.links = {}
        mock_response_webhook.json.return_value = [
            {"id": 1234, "url": "wrong_url"},
            {
                "id": 456,
                "url": "http://localhost:5000/api/workflows",
            },
        ]

        mock_requests_get = Mock()
        mock_requests_get.side_effect = [mock_response_projects, mock_response_webhook]

        mock_fetch = Mock()
        mock_fetch.return_value = UserSecrets(
            user_id=str(user0.id_),
            k8s_secret_name="gitlab_token",
            secrets=[
                Secret(name="gitlab_access_token", type_="env", value="gitlab_token")
            ],
        )
        with patch(
            "reana_server.gitlab_client.GitLabClient._request", mock_requests_get
        ), patch(
            "reana_commons.k8s.secrets.UserSecretsStore.fetch",
            mock_fetch,
        ):
            res = client.get(
                "/api/gitlab/projects",
                query_string={"access_token": user0.access_token},
            )

        assert res.status_code == 200
        assert res.json["has_prev"]
        assert not res.json["has_next"]
        assert res.json["total"] == 100
        assert len(res.json["items"]) == 1
        assert res.json["items"][0]["id"] == 123
        assert res.json["items"][0]["name"] == "qwerty"
        assert res.json["items"][0]["url"] == "url"
        assert res.json["items"][0]["path"] == "abcd"
        assert res.json["items"][0]["hook_id"] == 456
