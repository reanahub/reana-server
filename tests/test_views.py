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
from reana_db.database import Session
from reana_db.models import User, Workflow

from reana_server.utils import _create_and_associate_reana_user


def test_get_workflows(app, default_user, _get_user_mock):
    """Test get_workflows view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")()):
            res = client.get(url_for("workflows.get_workflows"),
                             query_string={"type": "batch"})
            assert res.status_code == 403

            res = client.get(url_for("workflows.get_workflows"),
                             query_string={"access_token":
                                           default_user.access_token,
                                           "type": "batch"})
            assert res.status_code == 200


def test_create_workflow(app, default_user, _get_user_mock):
    """Test create_workflow view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")()):
            res = client.post(url_for("workflows.create_workflow"))
            assert res.status_code == 403

            # remote repository given as spec, not implemented
            res = client.post(url_for("workflows.create_workflow"),
                              query_string={"access_token":
                                            default_user.access_token,
                                            "spec": "not_implemented"})
            assert res.status_code == 501

            # no specification provided
            res = client.post(url_for("workflows.create_workflow"),
                              query_string={"access_token":
                                            default_user.access_token})
            assert res.status_code == 500

            # unknown workflow engine
            workflow_data = {
                "workflow": {"specification": {}, "type": "unknown"},
                "workflow_name": "test"}
            res = client.post(url_for("workflows.create_workflow"),
                              headers={"Content-Type": "application/json"},
                              query_string={"access_token":
                                            default_user.access_token},
                              data=json.dumps(workflow_data))
            assert res.status_code == 500

            # name cannot be valid uuid4
            workflow_data['workflow']['type'] = 'serial'
            res = client.post(url_for("workflows.create_workflow"),
                              headers={"Content-Type": "application/json"},
                              query_string={"access_token":
                                            default_user.access_token,
                                            "workflow_name": str(uuid4())},
                              data=json.dumps(workflow_data))
            assert res.status_code == 400

            # wrong specification json
            workflow_data = {"nonsense": {"specification": {},
                                          "type": "unknown"}}
            res = client.post(url_for("workflows.create_workflow"),
                              headers={"Content-Type": "application/json"},
                              query_string={"access_token":
                                            default_user.access_token},
                              data=json.dumps(workflow_data))
            assert res.status_code == 400

            # correct case
            workflow_data = {"workflow": {"specification": {},
                                          "type": "serial"},
                             "workflow_name": "test"}
            res = client.post(url_for("workflows.create_workflow"),
                              headers={"Content-Type": "application/json"},
                              query_string={"access_token":
                                            default_user.access_token},
                              data=json.dumps(workflow_data))
            assert res.status_code == 200


def test_get_workflow_specification(app, default_user, _get_user_mock,
                                    sample_yadage_workflow_in_db):
    """Test get_workflow_specification view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")()):
            res = client.get(url_for("workflows.get_workflow_specification",
                                     workflow_id_or_name="1"))
            assert res.status_code == 403

            res = client.get(
                url_for("workflows.get_workflow_specification",
                        workflow_id_or_name=sample_yadage_workflow_in_db.id_),
                headers={"Content-Type": "application/json"},
                query_string={"access_token":
                              default_user.access_token},
                data=json.dumps(None))
            parsed_res = json.loads(res.data)
            assert res.status_code == 200
            specification = parsed_res['specification']
            assert specification['workflow']['specification'] == \
                sample_yadage_workflow_in_db.get_specification()
            assert specification['inputs']['parameters'] == \
                sample_yadage_workflow_in_db.get_input_parameters()
            assert specification['workflow']['type'] == \
                sample_yadage_workflow_in_db.type_


def test_get_workflow_logs(app, default_user, _get_user_mock):
    """Test get_workflow_logs view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")()):
            res = client.get(url_for("workflows.get_workflow_logs",
                                     workflow_id_or_name="1"))
            assert res.status_code == 403

            res = client.get(url_for("workflows.get_workflow_logs",
                                     workflow_id_or_name="1"),
                             headers={"Content-Type": "application/json"},
                             query_string={"access_token":
                                           default_user.access_token},
                             data=json.dumps(None))
            assert res.status_code == 200


def test_get_workflow_status(app, default_user, _get_user_mock):
    """Test get_workflow_logs view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")()):
            res = client.get(url_for("workflows.get_workflow_status",
                                     workflow_id_or_name="1"))
            assert res.status_code == 403

            res = client.get(url_for("workflows.get_workflow_status",
                                     workflow_id_or_name="1"),
                             headers={"Content-Type": "application/json"},
                             query_string={"access_token":
                                           default_user.access_token})
            assert res.status_code == 200


def test_set_workflow_status(app, default_user, _get_user_mock):
    """Test get_workflow_logs view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")()):
            res = client.put(url_for("workflows.set_workflow_status",
                                     workflow_id_or_name="1"))
            assert res.status_code == 403

            res = client.put(url_for("workflows.set_workflow_status",
                                     workflow_id_or_name="1"),
                             headers={"Content-Type": "application/json"},
                             query_string={"access_token":
                                           default_user.access_token})
            assert res.status_code == 500

            res = client.put(url_for("workflows.set_workflow_status",
                                     workflow_id_or_name="1"),
                             headers={"Content-Type": "application/json"},
                             query_string={"access_token":
                                           default_user.access_token,
                                           "status": "stop"},
                             data=json.dumps(dict(parameters=None)))
            assert res.status_code == 200


def test_upload_file(app, default_user, _get_user_mock):
    """Test upload_file view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.requests"):
            file_content = b"Upload this data."
            res = client.post(url_for("workflows.upload_file",
                                      workflow_id_or_name="1"),
                              query_string={"file_name": "test_upload.txt"},
                              input_stream=BytesIO(file_content))
            assert res.status_code == 403

            # wrong content type
            res = client.post(url_for("workflows.upload_file",
                                      workflow_id_or_name="1"),
                              query_string={"access_token":
                                            default_user.access_token,
                                            "file_name": "test_upload.txt"},
                              headers={"Content-Type":
                                       "multipart/form-data"},
                              input_stream=BytesIO(file_content))
            assert res.status_code == 400
            # missing file name
            res = client.post(url_for("workflows.upload_file",
                                      workflow_id_or_name="1"),
                              query_string={"access_token":
                                            default_user.access_token,
                                            "file_name": None},
                              headers={"Content-Type":
                                       "application/octet-stream"},
                              input_stream=BytesIO(file_content))
            assert res.status_code == 400

        requests_mock = Mock()
        requests_response_mock = Mock()
        requests_response_mock.status_code = 200
        requests_response_mock.json = \
            Mock(return_value={'message': 'File uploaded.'})
        requests_mock.post = Mock(return_value=requests_response_mock)
        with patch("reana_server.rest.workflows.requests",
                   requests_mock) as requests_client:
            res = client.post(url_for("workflows.upload_file",
                                      workflow_id_or_name="1"),
                              query_string={"access_token":
                                            default_user.access_token,
                                            "file_name":
                                            "test_upload.txt"},
                              headers={"Content-Type":
                                       "application/octet-stream"},
                              input_stream=BytesIO(file_content))
            requests_client.post.assert_called_once()
            assert file_content == \
                requests_client.post.call_args_list[0][1]['data'].read()

            # empty file
            res = client.post(url_for("workflows.upload_file",
                                      workflow_id_or_name="1"),
                              query_string={"access_token":
                                            default_user.access_token,
                                            "file_name": "empty.txt"},
                              headers={"Content-Type":
                                       "application/octet-stream"},
                              input_stream=BytesIO(b""))
            assert requests_client.post.call_count == 2
            data = requests_client.post.call_args_list[1][1]['data']
            assert not len(data)
            assert not data.read()


def test_download_file(app, default_user, _get_user_mock):
    """Test download_file view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.requests"):
            res = client.get(url_for("workflows.download_file",
                                     workflow_id_or_name="1",
                                     file_name="test_download"),
                             query_string={"file_name":
                                           "test_upload.txt"})
            assert res.status_code == 302

        requests_mock = Mock()
        requests_response_mock = Mock()
        requests_response_mock.status_code = 200
        requests_response_mock.json = \
            Mock(return_value={'message': 'File downloaded.'})
        requests_mock.get = Mock(return_value=requests_response_mock)
        with patch("reana_server.rest.workflows.requests",
                   requests_mock) as requests_client:
            res = client.get(
                url_for("workflows.download_file",
                        workflow_id_or_name="1",
                        file_name="test_download"),
                query_string={"access_token":
                              default_user.access_token})

            requests_client.get.assert_called_once()
            assert requests_client.get.return_value.status_code == 200


def test_delete_file(app, default_user, _get_user_mock):
    """Test delete_file view."""
    mock_response = Mock()
    mock_response.headers = {'Content-Type': 'multipart/form-data'}
    mock_response.json = Mock(return_value={})
    mock_response.status_code = 200
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")(
                       mock_http_response=mock_response)):
            res = client.delete(url_for("workflows.delete_file",
                                workflow_id_or_name="1",
                                file_name="test_delete.txt"))
            assert res.status_code == 403

            res = client.delete(url_for("workflows.delete_file",
                                        workflow_id_or_name="1",
                                        file_name="test_delete.txt"),
                                query_string={"access_token":
                                              default_user.access_token})
            assert res.status_code == 200


def test_get_files(app, default_user, _get_user_mock):
    """Test get_files view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")()):
            res = client.get(url_for("workflows.get_files",
                                     workflow_id_or_name="1"))
            assert res.status_code == 403

            res = client.get(url_for("workflows.get_files",
                                     workflow_id_or_name="1"),
                             query_string={"access_token":
                                           default_user.access_token})
            assert res.status_code == 500

        mock_http_response = Mock()
        mock_http_response.status_code = 200
        mock_http_response.json.return_value = dict(key='value')
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")(
                       mock_http_response=mock_http_response)):
            res = client.get(url_for("workflows.get_files",
                                     workflow_id_or_name="1"),
                             query_string={"access_token":
                                           default_user.access_token})
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


def test_user_login(app, default_user):
    """Test user_login view."""
    with app.test_client() as client:
        res = client.get(
            url_for("users.user_login")
        )
        assert res.status_code == 200
        assert json.loads(res.data)['message']


def test_move_files(app, default_user, _get_user_mock):
    """Test move_files view."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")()):
            res = client.put(
                url_for("workflows.move_files",
                        workflow_id_or_name="1"),
                query_string={"user": default_user.id_,
                              "source": "source.txt",
                              "target": "target.txt"})
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
                                  "target": "target.txt"})
                assert res.status_code == 200


@pytest.mark.parametrize(
    ('interactive_session_type', 'expected_status_code'),
    [(int_session_type, 200)
     for int_session_type in INTERACTIVE_SESSION_TYPES] +
    [('wrong-interactive-type', 404)])
def test_open_interactive_session(app, default_user,
                                  sample_serial_workflow_in_db,
                                  interactive_session_type,
                                  expected_status_code,
                                  _get_user_mock):
    """Test open interactive session."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")()):
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
                                   expected_status_code,
                                   _get_user_mock):
    """Test close an interactive session."""
    with app.test_client() as client:
        with patch("reana_server.rest.workflows.current_rwc_api_client",
                   make_mock_api_client("reana-workflow-controller")()):
            res = client.post(
                url_for(
                    "workflows.close_interactive_session",
                    workflow_id_or_name=sample_serial_workflow_in_db.id_),
                query_string={"access_token": default_user.access_token})
            assert res.status_code == expected_status_code


def test_create_and_associate_reana_user():
    user_email = 'test@reana.io'
    user_fullname = 'John Doe'
    username = 'johndoe'
    account_info = {'user': {'email': user_email,
                             'profile': {'full_name': user_fullname,
                                         'username': username}}}
    user = Session.query(User).filter_by(email=user_email).\
        one_or_none()
    assert user is None
    _create_and_associate_reana_user(None, account_info=account_info)
    user = Session.query(User).filter_by(email=user_email).\
        one_or_none()
    assert user
    assert user.email == user_email
    assert user.full_name == user_fullname
    assert user.username == username
