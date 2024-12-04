# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021, 2022, 2023, 2024 CERN.
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
from pytest_reana.test_utils import make_mock_api_client

from reana_db.models import User, InteractiveSessionType, RunStatus
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


def test_create_workflow(
    app, session, user0, _get_user_mock, sample_serial_workflow_in_db
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

            # no specification provided
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={"access_token": user0.access_token},
            )
            assert res.status_code == 500

            # unknown workflow engine
            workflow_specification = copy.deepcopy(
                sample_serial_workflow_in_db.reana_specification
            )
            workflow_specification["workflow"]["type"] = "unknown"
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={"Content-Type": "application/json"},
                query_string={
                    "access_token": user0.access_token,
                    "workflow_name": "test",
                },
                data=json.dumps(workflow_specification),
            )
            assert res.status_code == 500

            # name cannot be valid uuid4
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={"Content-Type": "application/json"},
                query_string={
                    "access_token": user0.access_token,
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
                headers={"Content-Type": "application/json"},
                query_string={
                    "access_token": user0.access_token,
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
                headers={"Content-Type": "application/json"},
                query_string={
                    "access_token": user0.access_token,
                    "workflow_name": "test",
                },
                data=json.dumps(workflow_specification),
            )
            assert res.status_code == 200

            # correct case
            workflow_specification = sample_serial_workflow_in_db.reana_specification
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={"Content-Type": "application/json"},
                query_string={
                    "access_token": user0.access_token,
                    "workflow_name": "test",
                },
                data=json.dumps(workflow_specification),
            )
            assert res.status_code == 200


def test_start_workflow_validates_specification(
    app, session, user0, sample_serial_workflow_in_db
):
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
            headers={"Content-Type": "application/json"},
            query_string={
                "access_token": user0.access_token,
            },
            data=json.dumps({}),
        )
        assert res.status_code == 400


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
                data=json.dumps(dict(parameters=None)),
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
