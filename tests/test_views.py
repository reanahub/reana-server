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
from io import BytesIO
from uuid import uuid4

import pytest
from flask import Flask, url_for
from mock import Mock, patch
from reana_commons.testing import make_mock_api_client

from reana_db.models import User, InteractiveSessionType, RunStatus
from reana_commons.k8s.secrets import UserSecrets, Secret

def test_get_workflows(app, user0, auth_headers):
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
                headers={"Authorization": "Bearer wrongtoken"},
                query_string={ "type": "batch"},
            )
            assert res.status_code == 401

            res = client.get(
                url_for("workflows.get_workflows"),
                headers=auth_headers(user0),
                query_string={
                    "type": "batch",
                },
            )
            assert res.status_code == 200


def test_create_workflow(
    app, session, user0, auth_headers, sample_serial_workflow_in_db
):
    """Test create_workflow view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.post(url_for("workflows.create_workflow"))
            assert res.status_code == 401

            res = client.post(
                url_for("workflows.create_workflow"),
                headers={"Authorization": "Bearer wrongtoken"},
            )
            assert res.status_code == 401

            # remote repository given as spec, not implemented
            res = client.post(
                url_for("workflows.create_workflow"),
                headers=auth_headers(user0),
                query_string={
                    "spec": "not_implemented",
                },
            )
            assert res.status_code == 501

            # no specification provided
            res = client.post(
                url_for("workflows.create_workflow"),
                headers=auth_headers(user0),
            )
            assert res.status_code == 500

            # unknown workflow engine
            workflow_specification = copy.deepcopy(
                sample_serial_workflow_in_db.reana_specification
            )
            workflow_specification["workflow"]["type"] = "unknown"
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={**auth_headers(user0), "Content-Type": "application/json"},
                query_string={
                    "workflow_name": "test",
                },
                data=json.dumps(workflow_specification),
            )
            assert res.status_code == 500

            # name cannot be valid uuid4
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={**auth_headers(user0), "Content-Type": "application/json"},
                query_string={
                    "workflow_name": str(uuid4()),
                },
                data=json.dumps(sample_serial_workflow_in_db.reana_specification),
            )
            assert res.status_code == 400

            # wrong specification json
            workflow_specification = {
                "nonsense": {"specification": {}, "type": "unknown"}
            }
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={**auth_headers(user0), "Content-Type": "application/json"},
                query_string={
                    "workflow_name": "test",
                },
                data=json.dumps(workflow_specification),
            )
            assert res.status_code == 400

            # not valid specification. but there is no validation
            workflow_specification = {
                "workflow": {"specification": {}, "type": "serial"},
            }
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={**auth_headers(user0), "Content-Type": "application/json"},
                query_string={
                    "workflow_name": "test",
                },
                data=json.dumps(workflow_specification),
            )
            assert res.status_code == 200

            # correct case
            workflow_specification = sample_serial_workflow_in_db.reana_specification
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={**auth_headers(user0), "Content-Type": "application/json"},
                query_string={
                    "workflow_name": "test",
                },
                data=json.dumps(workflow_specification),
            )
            assert res.status_code == 200


def test_start_workflow_validates_specification(
    app, session, user0, sample_serial_workflow_in_db, auth_headers):
    with app.test_client() as client:
        sample_serial_workflow_in_db.status = RunStatus.created
        sample_serial_workflow_in_db.name = "test"
        workflow_specification = copy.deepcopy(
            sample_serial_workflow_in_db.reana_specification
        )
        workflow_specification["workflow"]["type"] = "unknown"
        sample_serial_workflow_in_db.reana_specification = workflow_specification
        session.add(sample_serial_workflow_in_db)
        session.commit()
        res = client.post(
            url_for(
                "workflows.start_workflow",
                workflow_id_or_name=str(sample_serial_workflow_in_db.id_),
            ),
            headers={**auth_headers(user0), "Content-Type": "application/json"},
            data=json.dumps({}),
        )
        assert res.status_code == 400


def test_restart_workflow_validates_specification(
    app, session, user0, sample_serial_workflow_in_db, auth_headers):
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
            headers={**auth_headers(user0), "Content-Type": "application/json"},
            data=json.dumps(body),
        )
        assert res.status_code == 400


def test_info_surfaces_kubernetes_min_user_uid(app, user0, auth_headers):
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
                headers=auth_headers(user0),
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
    app, user0, auth_headers, sample_yadage_workflow_in_db
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
                headers={"Authorization": "Bearer wrongtoken"},
            )
            assert res.status_code == 401

            res = client.get(
                url_for(
                    "workflows.get_workflow_specification",
                    workflow_id_or_name=sample_yadage_workflow_in_db.id_,
                ),
                headers={**auth_headers(user0), "Content-Type": "application/json"},
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


def test_get_workflow_logs(app, user0, auth_headers):
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
                headers={"Authorization": "Bearer wrongtoken"},
            )
            assert res.status_code == 401

            res = client.get(
                url_for("workflows.get_workflow_logs", workflow_id_or_name="1"),
                headers={**auth_headers(user0), "Content-Type": "application/json"},
                data=json.dumps(None),
            )
            assert res.status_code == 200


def test_get_workflow_status(app, user0, auth_headers):
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
                headers={"Authorization": "Bearer wrongtoken"},
            )
            assert res.status_code == 401

            res = client.get(
                url_for("workflows.get_workflow_status", workflow_id_or_name="1"),
                headers={**auth_headers(user0), "Content-Type": "application/json"},
            )
            assert res.status_code == 200


def test_set_workflow_status(app, user0, auth_headers):
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
                headers={"Authorization": "Bearer wrongtoken"},
            )
            assert res.status_code == 401

            res = client.put(
                url_for("workflows.set_workflow_status", workflow_id_or_name="1"),
                headers={**auth_headers(user0), "Content-Type": "application/json"},
            )
            assert res.status_code == 422

            res = client.put(
                url_for("workflows.set_workflow_status", workflow_id_or_name="1"),
                headers={**auth_headers(user0), "Content-Type": "application/json"},
                query_string={
                    "status": "stop",
                },
                data=json.dumps({}),
            )
            assert res.status_code == 200


def test_upload_file(app, user0, auth_headers):
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
                headers={"Authorization": "Bearer wrongtoken"},
                query_string={
                    "file_name": "test_upload.txt",
                },
                input_stream=BytesIO(file_content),
            )
            assert res.status_code == 403

            # wrong content type
            res = client.post(
                url_for("workflows.upload_file", workflow_id_or_name="1"),
                query_string={
                    "file_name": "test_upload.txt",
                },
                headers={**auth_headers(user0), "Content-Type": "multipart/form-data"},
                input_stream=BytesIO(file_content),
            )
            assert res.status_code == 400
            # missing file name
            res = client.post(
                url_for("workflows.upload_file", workflow_id_or_name="1"),
                query_string={
                    "file_name": None,
                },
                headers={**auth_headers(user0), "Content-Type": "application/octet-stream"},
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
                    "file_name": "test_upload.txt",
                },
                headers={**auth_headers(user0), "Content-Type": "application/octet-stream"},
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
                    "file_name": "empty.txt",
                },
                headers={**auth_headers(user0), "Content-Type": "application/octet-stream"},
                input_stream=BytesIO(b""),
            )
            assert requests_client.post.call_count == 2
            data = requests_client.post.call_args_list[1][1]["data"]
            assert not len(data)
            assert not data.read()


def test_download_file(app, user0, auth_headers):
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
                headers={"Authorization": "Bearer wrongtoken"},
                query_string={
                    "file_name": "test_upload.txt",
                },
            )
            assert res.status_code == 401

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
                headers=auth_headers(user0),
            )

            requests_client.get.assert_called_once()
            assert requests_client.get.return_value.status_code == 200


def test_delete_file(app, user0, auth_headers):
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
                headers={"Authorization": "Bearer wrongtoken"},
            )
            assert res.status_code == 401

            res = client.delete(
                url_for(
                    "workflows.delete_file",
                    workflow_id_or_name="1",
                    file_name="test_delete.txt",
                ),
                headers=auth_headers(user0),
            )
            assert res.status_code == 200


def test_get_files(app, user0, auth_headers):
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
                headers={"Authorization": "Bearer wrongtoken"},
            )
            assert res.status_code == 401

            res = client.get(
                url_for("workflows.get_files", workflow_id_or_name="1"),
                headers=auth_headers(user0),
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
                headers=auth_headers(user0),
            )
            assert res.status_code == 200


def test_move_files(app, user0, auth_headers):
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
                headers={"Authorization": "Bearer wrongtoken"},
                query_string={
                    "user": user0.id_,
                    "source": "source.txt",
                    "target": "target.txt",
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
                headers=auth_headers(user0),
                    query_string={
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
    auth_headers,
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
                headers=auth_headers(user0),
            )
            assert res.status_code == expected_status_code


@pytest.mark.parametrize(("expected_status_code"), [200])
def test_close_interactive_session(
    app,
    user0,
    sample_serial_workflow_in_db,
    expected_status_code,
    auth_headers,
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
                headers=auth_headers(user0),
            )
            assert res.status_code == expected_status_code


def test_get_workflow_retention_rules(app, user0, auth_headers):
    """Test get_workflow_retention_rules."""
    endpoint_url = url_for(
        "workflows.get_workflow_retention_rules", workflow_id_or_name="workflow"
    )
    with app.test_client() as client:
        # Token not provided
        res = client.get(endpoint_url)
        assert res.status_code == 401

        # Token not valid
        res = client.get(endpoint_url, headers={"Authorization": "Bearer invalid_token"})
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
                endpoint_url, query_string={}
            , headers=auth_headers(user0))
            assert res.status_code == status_code
            assert "message" in res.json


def test_prune_workspace(app, user0, sample_serial_workflow_in_db, auth_headers):
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
        res = client.post(endpoint_url, headers={"Authorization": "Bearer invalid_token"})
        assert res.status_code == 403

        # Test invalid workflow name
        res = client.post(
            url_for(
                "workflows.prune_workspace",
                workflow_id_or_name="invalid_wf",
            ),
                headers=auth_headers(user0),
        )
        assert res.status_code == 403

        # Test normal behaviour
        status_code = 200
        res = client.post(
            endpoint_url, query_string={}
        , headers=auth_headers(user0))
        assert res.status_code == status_code
        assert "The workspace has been correctly pruned." in res.json["message"]

        res = client.post(
            endpoint_url,
            query_string={
                "include_inputs": True,
                "include_outputs": True,
            },
        headers=auth_headers(user0),
    )
        assert res.status_code == status_code
        assert "The workspace has been correctly pruned." in res.json["message"]


def test_gitlab_projects(app: Flask, user0, auth_headers):
    """Test fetching of GitLab projects."""
    with app.test_client() as client:
        # token not provided
        res = client.get("/api/gitlab/projects")
        assert res.status_code == 401

        # invalid REANA token
        res = client.get(
            "/api/gitlab/projects", headers={"Authorization": "Bearer invalid"}
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
            headers=auth_headers(user0),
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
            headers=auth_headers(user0),
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
