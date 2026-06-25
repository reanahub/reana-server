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
import yaml
from io import BytesIO
from uuid import uuid4

import pytest
from flask import Flask, url_for
from mock import Mock, patch
from reana_commons.testing import make_mock_api_client

from reana_db.models import User, InteractiveSessionType, RunStatus
from reana_commons.errors import REANAQuotaExceededError
from reana_commons.k8s.secrets import UserSecrets, Secret

from reana_server.utils import (
    _create_and_associate_local_user,
    _create_and_associate_oauth_user,
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
    return {"reana.yaml": (BytesIO(SERIAL_REANA_YAML.encode()), "reana.yaml")}


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


def _gitlab_create_patches(rwc_client):
    """Common mocks for driving the GitLab branch of ``create_workflow``."""
    spec = yaml.safe_load(SERIAL_REANA_YAML)
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
            "reana_server.rest.workflows.load_and_validate_spec",
            return_value=(spec, []),
        ),
        patch("reana_server.rest.workflows.get_disk_usage_or_zero", return_value=123),
    ]


def test_create_workflow_gitlab_accounts_disk_quota(
    app, session, user0, _get_user_mock, sample_serial_workflow_in_db, monkeypatch
):
    """A successful GitLab create charges the cloned workspace to disk quota."""
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {
            "workflow_id": str(sample_serial_workflow_in_db.id_),
            "workflow_name": sample_serial_workflow_in_db.name,
        },
        Mock(status_code=200),
    )
    _spec, patches = _gitlab_create_patches(rwc_client)
    with app.test_client() as client:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
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
    app, session, user0, _get_user_mock, sample_serial_workflow_in_db, monkeypatch
):
    """A GitLab create whose clone exceeds quota rolls the workflow back.

    The just-created workflow is marked ``deleted`` (no orphan), the GitLab build
    status is failed and the webhook is acknowledged, and nothing is accounted.
    """
    rwc_client = Mock()
    rwc_client.api.create_workflow.return_value.result.return_value = (
        {
            "workflow_id": str(sample_serial_workflow_in_db.id_),
            "workflow_name": sample_serial_workflow_in_db.name,
        },
        Mock(status_code=200),
    )
    _spec, patches = _gitlab_create_patches(rwc_client)
    with app.test_client() as client:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
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
    assert sample_serial_workflow_in_db.status == RunStatus.deleted
    fail_build_mock.assert_called_once()
    store_mock.assert_not_called()
    update_mock.assert_not_called()


def test_validate_workflow_specification_environment_check(
    app, user0, _get_user_mock, monkeypatch, tmp_path
):
    """The ``environments`` flag drives the optional image check wiring.

    The cheap registry check is exercised in reana-commons; here we assert the
    endpoint wiring: it runs only when requested, appends the findings, returns
    the image list + runtime UID/GID for the client's deep ``--pull`` checks,
    and skips the registry existence lookup when the client will pull locally.
    """
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", str(tmp_path))
    finding = {"code": "image_tag", "message": "boom", "path": "img:1"}
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.check_spec_environments",
            return_value=[finding],
        ) as env_mock:
            # Without the flag the check is not run and no image data is added.
            res = client.post(
                url_for("workflows.validate_workflow_specification"),
                query_string={"access_token": user0.access_token},
                data=_serial_bundle(),
                content_type="multipart/form-data",
            )
            assert res.status_code == 200
            env_mock.assert_not_called()
            assert "images" not in res.json

            # --environments only: server checks existence + returns image data.
            res = client.post(
                url_for("workflows.validate_workflow_specification"),
                query_string={
                    "access_token": user0.access_token,
                    "environments": "true",
                },
                data=_serial_bundle(),
                content_type="multipart/form-data",
            )
            assert res.status_code == 200
            assert env_mock.call_args.kwargs.get("check_existence") is True
            assert finding in res.json["warnings"]
            assert "images" in res.json
            assert isinstance(res.json["runtime_uid"], int)
            assert isinstance(res.json["runtime_gid"], int)

            # --pull: the client is authoritative on existence, so the server
            # skips the registry lookup (check_existence=False).
            res = client.post(
                url_for("workflows.validate_workflow_specification"),
                query_string={
                    "access_token": user0.access_token,
                    "environments": "true",
                    "pull": "true",
                },
                data=_serial_bundle(),
                content_type="multipart/form-data",
            )
            assert res.status_code == 200
            assert env_mock.call_args.kwargs.get("check_existence") is False


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


def test_restart_workflow_validates_specification(
    app, session, user0, sample_serial_workflow_in_db
):
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


def test_restart_workflow_does_not_publish_reana_specification(
    app, session, user0, sample_serial_workflow_in_db
):
    """Replacement restart strips server-only spec payload before scheduling."""
    with app.test_client() as client:
        sample_serial_workflow_in_db.status = RunStatus.finished
        sample_serial_workflow_in_db.name = "test"
        session.add(sample_serial_workflow_in_db)
        session.commit()

        body = {
            "reana_specification": copy.deepcopy(
                sample_serial_workflow_in_db.reana_specification
            ),
            "restart": True,
        }
        with patch(
            "reana_server.rest.workflows.publish_workflow_submission"
        ) as publish_mock:
            res = client.post(
                url_for("workflows.start_workflow", workflow_id_or_name="test"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": user0.access_token},
                data=json.dumps(body),
            )

    assert res.status_code == 200
    assert publish_mock.call_args.args[2] == {"restart": True}


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
        requests_mock.post = Mock(return_value=requests_response_mock)
        with patch(
            "reana_server.rest.workflows.requests", requests_mock
        ) as requests_client:
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
            assert (
                file_content == requests_client.post.call_args_list[0][1]["data"].read()
            )

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
            assert not data.read()


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
