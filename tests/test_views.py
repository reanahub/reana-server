# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test server views."""

import json
from io import BytesIO
from uuid import uuid4

import pytest
from flask import url_for
from jsonschema.exceptions import ValidationError
from mock import Mock, PropertyMock, patch
from pytest_reana.fixtures import default_user
from pytest_reana.test_utils import make_mock_api_client
from reana_commons.config import INTERACTIVE_SESSION_TYPES


def test_get_workflows(app, default_user):
    """Test get_workflows view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(
                url_for("workflows.get_workflows"),
                query_string={"user_id": default_user.id_,
                              "type": "batch"},
            )
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.get_workflows"),
                query_string={"access_token": default_user.access_token,
                              "type": "batch"},
            )
            assert res.status_code == 200


def test_create_workflow(app, default_user):
    """Test create_workflow view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            # access token needs to be passed instead of user_id
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={"user_id": default_user.id_},
            )
            assert res.status_code == 403

            # remote repository given as spec, not implemented
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={"access_token": default_user.access_token,
                              "spec": "not_implemented"},
            )
            assert res.status_code == 501

            # no specification provided
            res = client.post(
                url_for("workflows.create_workflow"),
                query_string={"access_token": default_user.access_token},
            )
            assert res.status_code == 500

            # unknown workflow engine
            workflow_data = {
                "workflow": {"specification": {}, "type": "unknown"},
                "workflow_name": "test",
            }
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": default_user.access_token},
                data=json.dumps(workflow_data),
            )
            assert res.status_code == 500

            # name cannot be valid uuid4
            workflow_data['workflow']['type'] = 'serial'
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": default_user.access_token,
                              "workflow_name": str(uuid4())},
                data=json.dumps(workflow_data),
            )
            assert res.status_code == 400

            # wrong specification json
            workflow_data = {
                "nonsense": {"specification": {}, "type": "unknown"},
            }
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": default_user.access_token},
                data=json.dumps(workflow_data),
            )
            assert res.status_code == 400

            # correct case
            workflow_data = {
                "workflow": {"specification": {}, "type": "serial"},
                "workflow_name": "test",
            }
            res = client.post(
                url_for("workflows.create_workflow"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": default_user.access_token},
                data=json.dumps(workflow_data),
            )
            assert res.status_code == 200


def test_get_workflow_logs(app, default_user):
    """Test get_workflow_logs view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(
                url_for("workflows.get_workflow_logs",
                        workflow_id_or_name="1"),
                query_string={"user_id": default_user.id_},
            )
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.get_workflow_logs",
                        workflow_id_or_name="1"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": default_user.access_token},
            )
            assert res.status_code == 200


def test_get_workflow_status(app, default_user):
    """Test get_workflow_logs view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(
                url_for("workflows.get_workflow_status",
                        workflow_id_or_name="1"),
                query_string={"user_id": default_user.id_},
            )
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.get_workflow_status",
                        workflow_id_or_name="1"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": default_user.access_token},
            )
            assert res.status_code == 200


def test_set_workflow_status(app, default_user):
    """Test get_workflow_logs view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.put(
                url_for("workflows.set_workflow_status",
                        workflow_id_or_name="1"),
                query_string={"user_id": default_user.id_},
            )
            assert res.status_code == 403

            res = client.put(
                url_for("workflows.set_workflow_status",
                        workflow_id_or_name="1"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": default_user.access_token},
            )
            assert res.status_code == 500

            res = client.put(
                url_for("workflows.set_workflow_status",
                        workflow_id_or_name="1"),
                headers={"Content-Type": "application/json"},
                query_string={"access_token": default_user.access_token,
                              "status": "stop"},
                data=json.dumps(dict(parameters=None))
            )
            assert res.status_code == 200


def test_upload_file(app, default_user):
    """Test upload_file view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.post(
                url_for("workflows.upload_file",
                        workflow_id_or_name="1"),
                query_string={"user_id": default_user.id_,
                              "file_name": "test_upload.txt"},
                data={
                    "file_content": "tests/test_files/test_upload.txt"
                }
            )
            assert res.status_code == 403

            res = client.post(
                url_for("workflows.upload_file",
                        workflow_id_or_name="1"),
                query_string={"access_token": default_user.access_token,
                              "file_name": "test_upload.txt"},
                headers={"content_type": "multipart/form-data"},
                data={
                    "file": (BytesIO(b"Upload this data."),
                             "tests/test_files/test_upload.txt")
                }
            )
            assert res.status_code == 400

            res = client.post(
                url_for("workflows.upload_file",
                        workflow_id_or_name="1"),
                query_string={"access_token": default_user.access_token,
                              "file_name": None},
                headers={"content_type": "multipart/form-data"},
                data={
                    "file_content": (BytesIO(b"Upload this data."),
                                     "tests/test_files/test_upload.txt")
                }
            )
            assert res.status_code == 400

            res = client.post(
                url_for("workflows.upload_file",
                        workflow_id_or_name="1"),
                query_string={"access_token": default_user.access_token,
                              "file_name": "test_upload.txt"},
                headers={"content_type": "multipart/form-data"},
                data={
                    "file_content": (BytesIO(b"Upload this data."),
                                     "tests/test_files/test_upload.txt")
                }
            )
            assert res.status_code == 200


def test_download_file(app, default_user):
    """Test download_file view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(
                url_for("workflows.download_file",
                        workflow_id_or_name="1",
                        file_name="test_download"),
                query_string={"user_id": default_user.id_,
                              "file_name": "test_upload.txt"},
            )
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.download_file",
                        workflow_id_or_name="1",
                        file_name="test_download"),
                query_string={"access_token": default_user.access_token},
            )
            assert res.status_code == 200


def test_delete_file(app, default_user):
    """Test delete_file view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(
                url_for("workflows.delete_file",
                        workflow_id_or_name="1",
                        file_name="test_delete.txt"),
                query_string={"user_id": default_user.id_})
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.delete_file",
                        workflow_id_or_name="1",
                        file_name="test_delete.txt"),
                query_string={"access_token": default_user.access_token},
            )
            assert res.status_code == 200


def test_get_files(app, default_user):
    """Test get_files view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.get(
                url_for("workflows.get_files",
                        workflow_id_or_name="1"),
                query_string={"user_id": default_user.id_},
            )
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.get_files",
                        workflow_id_or_name="1"),
                query_string={"access_token": default_user.access_token},
            )
            assert res.status_code == 500

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = dict(key='value')
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(
                mock_http_response=mock_response),
        ):
            res = client.get(
                url_for("workflows.get_files",
                        workflow_id_or_name="1"),
                query_string={"access_token": default_user.access_token},
            )
            assert res.status_code == 200


def test_get_user(app, default_user):
    """Test get_user view."""
    with app.test_client() as client:
        res = client.get(
            url_for("users.get_user"),
            query_string={"id_": default_user.id_,
                          "email": default_user.email,
                          "user_token": default_user.access_token},
        )
        assert res.status_code == 403

        res = client.get(
            url_for("users.get_user"),
            query_string={"id_": default_user.id_,
                          "email": default_user.email,
                          "access_token": default_user.access_token},
        )
        assert res.status_code == 200


def test_create_user(app, default_user):
    """Test create_user view."""
    with app.test_client() as client:
        res = client.post(
            url_for("users.create_user"),
            query_string={"id_": default_user.id_,
                          "email": default_user.email,
                          "user_token": default_user.access_token},
        )
        assert res.status_code == 403

        res = client.post(
            url_for("users.create_user"),
            query_string={"id_": default_user.id_,
                          "email": default_user.email,
                          "access_token": default_user.access_token},
        )
        assert res.status_code == 403

    with app.test_client() as client:
        res = client.post(
            url_for("users.create_user"),
            query_string={"email": "test_email",
                          "access_token": default_user.access_token},
        )
        assert res.status_code == 201


def test_move_files(app, default_user):
    """Test move_files view."""
    with app.test_client() as client:
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(),
        ):
            res = client.put(
                url_for("workflows.move_files",
                        workflow_id_or_name="1"),
                query_string={"user": default_user.id_,
                              "source": "source.txt",
                              "target": "target.txt",
                              })
            assert res.status_code == 403

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = dict(key='value')
        with patch(
            "reana_server.rest.workflows.current_rwc_api_client",
            make_mock_api_client("reana-workflow-controller")(
                mock_http_response=mock_response),
        ):
            res = client.put(
                url_for("workflows.move_files",
                        workflow_id_or_name="1"),
                query_string={"access_token": default_user.access_token,
                              "source": "source.txt",
                              "target": "target.txt",
                              })
            assert res.status_code == 200


@pytest.mark.parametrize(
    ('interactive_session_type', 'expected_status_code'),
    [(int_session_type, 200)
     for int_session_type in INTERACTIVE_SESSION_TYPES] +
    [('wrong-interactive-type', 404)])
def test_open_interactive_session(app, default_user,
                                  sample_serial_workflow_in_db,
                                  interactive_session_type,
                                  expected_status_code):
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
                        interactive_session_type=interactive_session_type),
                    query_string={"access_token": default_user.access_token})
                assert res.status_code == expected_status_code


@pytest.mark.parametrize(
    ('expected_status_code'), [200])
def test_close_interactive_session(app, default_user,
                                   sample_serial_workflow_in_db,
                                   expected_status_code):
    """Test close an interactive session."""
    with app.test_client() as client:
            with patch(
                "reana_server.rest.workflows.current_rwc_api_client",
                make_mock_api_client("reana-workflow-controller")(),
            ):
                res = client.post(
                    url_for(
                        "workflows.close_interactive_session",
                        workflow_id_or_name=sample_serial_workflow_in_db.id_),
                    query_string={"access_token": default_user.access_token})
                assert res.status_code == expected_status_code
