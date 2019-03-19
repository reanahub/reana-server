# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Reana-Server workflow-functionality Flask-Blueprint."""
import io
import logging
import subprocess
import traceback

import fs
from bravado.exception import HTTPError
from flask import Blueprint
from flask import current_app as app
from flask import jsonify, request, send_file
from reana_commons.config import INTERACTIVE_SESSION_TYPES
from reana_commons.utils import get_workspace_disk_usage
from reana_db.database import Session
from reana_db.models import Workflow, WorkflowStatus
from reana_db.utils import _get_workflow_with_uuid_or_name

from reana_server.api_client import current_rwc_api_client, \
    current_workflow_submission_publisher
from reana_server.config import SHARED_VOLUME_PATH
from reana_server.utils import get_user_from_token, is_uuid_v4

blueprint = Blueprint('workflows', __name__)


@blueprint.route('/workflows', methods=['GET'])
def get_workflows():  # noqa
    r"""Get all current workflows in REANA.

    ---
    get:
      summary: Returns list of all current workflows in REANA.
      description: >-
        This resource return all current workflows in JSON format.
      operationId: get_workflows
      produces:
       - application/json
      parameters:
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
        - name: type
          in: query
          description: Required. Type of workflows.
          required: true
          type: string
        - name: verbose
          in: query
          description: Optional flag to show more information.
          required: false
          type: boolean
      responses:
        200:
          description: >-
            Request succeeded. The response contains the list of all workflows.
          schema:
            type: array
            items:
              type: object
              properties:
                id:
                  type: string
                name:
                  type: string
                status:
                  type: string
                size:
                  type: string
                user:
                  type: string
                created:
                  type: string
          examples:
            application/json:
              [
                {
                  "id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                  "name": "mytest.1",
                  "status": "running",
                  "size": "10M",
                  "user": "00000000-0000-0000-0000-000000000000",
                  "created": "2018-06-13T09:47:35.66097",
                },
                {
                  "id": "3c9b117c-d40a-49e3-a6de-5f89fcada5a3",
                  "name": "mytest.2",
                  "status": "finished",
                  "size": "12M",
                  "user": "00000000-0000-0000-0000-000000000000",
                  "created": "2018-06-13T09:47:35.66097",
                },
                {
                  "id": "72e3ee4f-9cd3-4dc7-906c-24511d9f5ee3",
                  "name": "mytest.3",
                  "status": "created",
                  "size": "180K",
                  "user": "00000000-0000-0000-0000-000000000000",
                  "created": "2018-06-13T09:47:35.66097",
                },
                {
                  "id": "c4c0a1a6-beef-46c7-be04-bf4b3beca5a1",
                  "name": "mytest.4",
                  "status": "created",
                  "size": "1G",
                  "user": "00000000-0000-0000-0000-000000000000",
                  "created": "2018-06-13T09:47:35.66097",
                }
              ]
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000 does not
                            exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
          examples:
            application/json:
              {
                "message": "Something went wrong."
              }
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))
        type = request.args.get('type', 'batch')
        verbose = request.args.get('verbose', False)
        response, http_response = current_rwc_api_client.api.\
            get_workflows(
                user=user_id,
                type=type,
                verbose=bool(verbose)).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows', methods=['POST'])
def create_workflow():  # noqa
    r"""Create a workflow.

    ---
    post:
      summary: Creates a new workflow based on a REANA specification file.
      description: >-
        This resource is expecting a REANA specification in JSON format with
        all the necessary information to instantiate a workflow.
      operationId: create_workflow
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_name
          in: query
          description: Name of the workflow to be created. If not provided
            name will be generated.
          required: true
          type: string
        # probably need to rename this to something more specific
        - name: spec
          in: query
          description: Remote repository which contains a valid REANA
            specification.
          required: false
          type: string
        - name: reana_specification
          in: body
          description: REANA specification with necessary data to instantiate
            a workflow.
          required: false
          schema:
            type: object
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
      responses:
        201:
          description: >-
            Request succeeded. The workflow has been created.
          schema:
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
          examples:
            application/json:
              {
                "message": "The workflow has been successfully created.",
                "workflow_id": "cdcf48b1-c2f3-4693-8230-b066e088c6ac",
                "workflow_name": "mytest.1"
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000 does not
                            exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
        501:
          description: >-
            Request failed. Not implemented.
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))
        if request.json:
            # validate against schema
            reana_spec_file = request.json
            workflow_engine = reana_spec_file['workflow']['type']
        elif request.args.get('spec'):
            return jsonify('Not implemented'), 501
        else:
            raise Exception('Either remote repository or a reana spec need to \
            be provided')

        if workflow_engine not in app.config['AVAILABLE_WORKFLOW_ENGINES']:
            raise Exception('Unknown workflow type.')

        workflow_name = request.args.get('workflow_name', '')

        if is_uuid_v4(workflow_name):
            return jsonify({'message':
                            'Workflow name cannot be a valid UUIDv4.'}), \
                400
        workflow_dict = {'reana_specification': reana_spec_file,
                         'workflow_name': workflow_name}
        workflow_dict['operational_options'] = \
            reana_spec_file.get('inputs', {}).get('options', {})
        response, http_response = current_rwc_api_client.api.\
            create_workflow(
                workflow=workflow_dict,
                user=user_id).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name>/logs', methods=['GET'])
def get_workflow_logs(workflow_id_or_name):  # noqa
    r"""Get workflow logs.

    ---
    get:
      summary: Get workflow logs of a workflow.
      description: >-
        This resource reports the status of a workflow.
        Resource is expecting a workflow UUID.
      operationId: get_workflow_logs
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: Required. API access_token of workflow owner.
          required: true
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Info about a workflow, including the status is
            returned.
          schema:
            type: object
            properties:
              workflow_id:
                type: string
              workflow_name:
                type: string
              logs:
                type: string
              user:
                type: string
          examples:
            application/json:
              {
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
                "logs": "<Workflow engine log output>",
                "user": "00000000-0000-0000-0000-000000000000"
              }
        400:
          description: >-
            Request failed. The incoming data specification seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          examples:
            application/json:
              {
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist"
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.\
            get_workflow_logs(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name>/status', methods=['GET'])
def get_workflow_status(workflow_id_or_name):  # noqa
    r"""Get workflow status.

    ---
    get:
      summary: Get status of a workflow.
      description: >-
        This resource reports the status of a workflow.
        Resource is expecting a workflow UUID.
      operationId: get_workflow_status
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Info about a workflow, including the status is
            returned.
          schema:
            type: object
            properties:
              id:
                type: string
              name:
                type: string
              created:
                type: string
              status:
                type: string
              user:
                type: string
              progress:
                type: object
              logs:
                type: string
          examples:
            application/json:
              {
                "created": "2018-10-29T12:50:12",
                "id": "4e576cf9-a946-4346-9cde-7712f8dcbb3f",
                "logs": "",
                "name": "mytest.1",
                "progress": {
                  "current_command": None,
                  "current_step_name": None,
                  "failed": {"job_ids": [], "total": 0},
                  "finished": {"job_ids": [], "total": 0},
                  "run_started_at": "2018-10-29T12:51:04",
                  "running": {"job_ids": [], "total": 0},
                  "total": {"job_ids": [], "total": 1}
                },
                "status": "running",
                "user": "00000000-0000-0000-0000-000000000000"
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either User or Analysis does not exist.
          examples:
            application/json:
              {
                "message": "Analysis 256b25f4-4cfb-4684-b7a8-73872ef455a1 does
                            not exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.\
            get_workflow_status(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name>/start', methods=['POST'])
def start_workflow(workflow_id_or_name):  # noqa
    r"""Start workflow.
    ---
    post:
      summary: Start workflow.
      description: >-
        This resource starts the workflow execution process.
        Resource is expecting a workflow UUID.
      operationId: start_workflow
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
        - name: parameters
          in: body
          description: >-
            Optional. Additional input parameters and operational options.
          required: false
          schema:
            type: object
      responses:
        200:
          description: >-
            Request succeeded. Info about a workflow, including the execution
            status is returned.
          schema:
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
              status:
                type: string
              user:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow submitted",
                "id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
                "status": "queued",
                "user": "00000000-0000-0000-0000-000000000000"
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either User or Workflow does not exist.
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1
                            does not exist"
              }
        409:
          description: >-
            Request failed. The workflow could not be started due to a
            conflict.
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1
                            could not be started because it is already
                            running."
              }
        500:
          description: >-
            Request failed. Internal controller error.
        501:
          description: >-
            Request failed. The specified status change is not implemented.
          examples:
            application/json:
              {
                "message": "Status resume is not supported yet."
              }
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")
        parameters = request.json
        workflow = _get_workflow_with_uuid_or_name(
            workflow_id_or_name, user_id)
        if workflow.status != WorkflowStatus.created:
            raise ValueError("Workflow cannot be started again.")
        Workflow.update_workflow_status(Session, workflow.id_,
                                        WorkflowStatus.queued)
        current_workflow_submission_publisher.publish_workflow_submission(
            user_id=user_id,
            workflow_id_or_name=workflow_id_or_name,
            parameters=parameters
        )
        response = {'message': 'Workflow submitted.',
                    'workflow_id': workflow_id_or_name,
                    'workflow_name': workflow_id_or_name,
                    'status': WorkflowStatus.queued.name,
                    'user': user_id}
        return jsonify(response), 200
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name>/status', methods=['PUT'])
def set_workflow_status(workflow_id_or_name):  # noqa
    r"""Set workflow status.
    ---
    put:
      summary: Set status of a workflow.
      description: >-
        This resource reports the status of a workflow.
        Resource is expecting a workflow UUID.
      operationId: set_workflow_status
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: status
          in: query
          description: Required. New workflow status.
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
        - name: parameters
          in: body
          description: >-
            Optional. Additional input parameters and operational options.
          required: false
          schema:
            type: object
      responses:
        200:
          description: >-
            Request succeeded. Info about a workflow, including the status is
            returned.
          schema:
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
              status:
                type: string
              user:
                type: string
          examples:
            application/json:
              {
                "message": "Workflow successfully launched",
                "id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
                "status": "created",
                "user": "00000000-0000-0000-0000-000000000000"
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either User or Workflow does not exist.
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1
                            does not exist"
              }
        409:
          description: >-
            Request failed. The workflow could not be started due to a
            conflict.
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1
                            could not be started because it is already
                            running."
              }
        500:
          description: >-
            Request failed. Internal controller error.
        501:
          description: >-
            Request failed. The specified status change is not implemented.
          examples:
            application/json:
              {
                "message": "Status resume is not supported yet."
              }
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")
        status = request.args.get('status')
        parameters = request.json
        response, http_response = current_rwc_api_client.api.\
            set_workflow_status(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name,
                status=status,
                parameters=parameters).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name>/workspace',
                 methods=['POST'])
def upload_file(workflow_id_or_name):  # noqa
    r"""Upload file to workspace.

    ---
    post:
      summary: Adds a file to the workspace.
      description: >-
        This resource is expecting a file to place in the workspace.
      operationId: upload_file
      consumes:
        - multipart/form-data
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: file_content
          in: formData
          description: >-
            Required. File to be transferred to the workflow workspace.
          required: true
          type: file
        - name: file_name
          in: query
          description: Required. File name.
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. File successfully transferred.
          schema:
            type: object
            properties:
              message:
                type: string
        400:
          description: >-
            Request failed. The incoming payload seems malformed
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          examples:
            application/json:
              {
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist"
              }
        500:
          description: >-
            Request failed. Internal server error.
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        file_ = request.files['file_content'].stream.read()
        response, http_response = current_rwc_api_client.api.\
            upload_file(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name,
                file_content=file_,
                file_name=request.args['file_name']).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route(
    '/workflows/<workflow_id_or_name>/workspace/<path:file_name>',
    methods=['GET'])
def download_file(workflow_id_or_name, file_name):  # noqa
    r"""Download a file from the workspace.

    ---
    get:
      summary: Returns the requested file.
      description: >-
        This resource is expecting a workflow UUID and a file name existing
        inside the workspace to return its content.
      operationId: download_file
      produces:
        - multipart/form-data
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. workflow UUID or name.
          required: true
          type: string
        - name: file_name
          in: path
          description: Required. Name (or path) of the file to be downloaded.
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Requests succeeded. The file has been downloaded.
          schema:
            type: file
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. `file_name` does not exist .
          examples:
            application/json:
              {
                "message": "input.csv does not exist"
              }
        500:
          description: >-
            Request failed. Internal server error.
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.\
            download_file(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name,
                file_name=file_name).result()

        return send_file(
            io.BytesIO(http_response.raw_bytes),
            attachment_filename=file_name,
            mimetype='multipart/form-data'), 200
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route(
    '/workflows/<workflow_id_or_name>/workspace/<path:file_name>',
    methods=['DELETE'])
def delete_file(workflow_id_or_name, file_name):  # noqa
    r"""Delete a file from the workspace.

    ---
    delete:
      summary: Delete the specified file.
      description: >-
        This resource is expecting a workflow UUID and a filename existing
        inside the workspace to be deleted.
      operationId: delete_file
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Workflow UUID or name
          required: true
          type: string
        - name: file_name
          in: path
          description: Required. Name (or path) of the file to be deleted.
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Requests succeeded. The file has been downloaded.
          schema:
            type: file
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. `file_name` does not exist.
          examples:
            application/json:
              {
                "message": "input.csv does not exist"
              }
        500:
          description: >-
            Request failed. Internal server error.
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.\
            delete_file(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name,
                file_name=file_name).result()

        return jsonify(http_response.json()), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name>/workspace',
                 methods=['GET'])
def get_files(workflow_id_or_name):  # noqa
    r"""List all files contained in a workspace.

    ---
    get:
      summary: Returns the workspace file list.
      description: >-
        This resource retrieves the file list of a workspace, given
        its workflow UUID.
      operationId: get_files
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Requests succeeded. The list of files has been retrieved.
          schema:
            type: array
            items:
              type: object
              properties:
                name:
                  type: string
                last-modified:
                  type: string
                size:
                  type: integer
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Analysis does not exist.
          examples:
            application/json:
              {
                "message": "Analysis 256b25f4-4cfb-4684-b7a8-73872ef455a1 does
                            not exist."
              }
        500:
          description: >-
            Request failed. Internal server error.
          examples:
            application/json:
              {
                "message": "Internal server error."
              }
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.\
            get_files(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name).result()

        return jsonify(http_response.json()), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name>/parameters',
                 methods=['GET'])
def get_workflow_parameters(workflow_id_or_name):  # noqa
    r"""Get workflow input parameters.

    ---
    get:
      summary: Get parameters of a workflow.
      description: >-
        This resource reports the input parameters of a workflow.
        Resource is expecting a workflow UUID.
      operationId: get_workflow_parameters
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Workflow input parameters, including the status
            are returned.
          schema:
            type: object
            properties:
              id:
                type: string
              name:
                type: string
              type:
                type: string
              parameters:
                type: object
          examples:
            application/json:
              {
                'id': 'dd4e93cf-e6d0-4714-a601-301ed97eec60',
                'name': 'workflow.24',
                'type': 'serial',
                'parameters': {'helloworld': 'code/helloworld.py',
                               'inputfile': 'data/names.txt',
                               'outputfile': 'results/greetings.txt',
                               'sleeptime': 2}
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either User or Analysis does not exist.
          examples:
            application/json:
              {
                "message": "Analysis 256b25f4-4cfb-4684-b7a8-73872ef455a1 does
                            not exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.\
            get_workflow_parameters(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name_a>/diff/'
                 '<workflow_id_or_name_b>', methods=['GET'])
def get_workflow_diff(workflow_id_or_name_a, workflow_id_or_name_b):  # noqa
    r"""Get differences between two workflows.

    ---
    get:
      summary: Get diff between two workflows.
      description: >-
        This resource shows the differences between
        the assets of two workflows.
        Resource is expecting two workflow UUIDs or names.
      operationId: get_workflow_diff
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name_a
          in: path
          description: Required. Analysis UUID or name of the first workflow.
          required: true
          type: string
        - name: workflow_id_or_name_b
          in: path
          description: Required. Analysis UUID or name of the second workflow.
          required: true
          type: string
        - name: brief
          in: query
          description: Optional flag. If set, file contents are examined.
          required: false
          type: boolean
          default: false
        - name: context_lines
          in: query
          description: Optional parameter. Sets number of context lines
                       for workspace diff output.
          required: false
          type: string
          default: '5'
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Info about a workflow, including the status is
            returned.
          schema:
            type: object
            properties:
              reana_specification:
                type: string
              workspace_listing:
                type: string
          examples:
            application/json:
              {
                "reana_specification":
                ["- nevents: 100000\n+ nevents: 200000"],
                "workspace_listing": {"Only in workspace a: code"}
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either user or workflow does not exist.
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1 does
                            not exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))
        brief = request.args.get('brief', False)
        brief = True if brief == 'true' else False
        context_lines = request.args.get('context_lines', 5)
        if not workflow_id_or_name_a or not workflow_id_or_name_b:
            raise ValueError("Workflow id or name is not supplied")

        response, http_response = current_rwc_api_client.api. \
            get_workflow_diff(
                user=user_id,
                brief=brief,
                context_lines=context_lines,
                workflow_id_or_name_a=workflow_id_or_name_a,
                workflow_id_or_name_b=workflow_id_or_name_b).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name>/open/'
                 '<interactive_session_type>',
                 methods=['POST'])
def open_interactive_session(workflow_id_or_name,
                             interactive_session_type):  # noqa
    r"""Start an interactive session inside the workflow workspace.

    ---
    post:
      summary: Start an interactive session inside the workflow workspace.
      description: >-
        This resource is expecting a workflow to start an interactive session
        within its workspace.
      operationId: open_interactive_session
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Workflow UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
        - name: interactive_session_type
          in: path
          description: Type of interactive session to use.
          required: true
          type: string
        - name: interactive_session_configuration
          in: body
          description: >-
            Interactive session configuration.
          required: false
          schema:
            type: object
            properties:
              image:
                type: string
                description: >-
                  Replaces the default Docker image of an interactive session.
      responses:
        200:
          description: >-
            Request succeeded. The interactive session has been opened.
          schema:
            type: object
            properties:
              path:
                type: string
          examples:
            application/json:
              {
                "path": "/dd4e93cf-e6d0-4714-a601-301ed97eec60",
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either user or workflow does not exist.
          examples:
            application/json:
              {
                "message": "Interactive session type jupiter not found, try
                            with one of: [jupyter]."
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))
        if interactive_session_type not in INTERACTIVE_SESSION_TYPES:
            return jsonify({
                "message": "Interactive session type {0} not found, try "
                           "with one of: {1}".format(
                               interactive_session_type,
                               INTERACTIVE_SESSION_TYPES)}), 404
        if not workflow_id_or_name:
            raise KeyError("workflow_id_or_name is not supplied")

        response, http_response = current_rwc_api_client.api.\
            open_interactive_session(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name,
                interactive_session_type=interactive_session_type,
                interactive_session_configuration=request.json or {}).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name>/close/',
                 methods=['POST'])
def close_interactive_session(workflow_id_or_name):  # noqa
    r"""Close an interactive workflow session.

    ---
    post:
      summary: Close an interactive workflow session.
      description: >-
        This resource is expecting a workflow to close an interactive session
        within its workspace.
      operationId: close_interactive_session
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Workflow UUID or name.
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. The interactive session has been closed.
          schema:
            type: object
            properties:
              path:
                type: string
          examples:
            application/json:
              {
                "message": "The interactive session has been closed",
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either user or workflow does not exist.
          examples:
            application/json:
              {
                "message": "Either user or workflow does not exist."
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))
        if not workflow_id_or_name:
            raise KeyError("workflow_id_or_name is not supplied")
        response, http_response = current_rwc_api_client.api.\
            close_interactive_session(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/move_files/<workflow_id_or_name>',
                 methods=['PUT'])
def move_files(workflow_id_or_name):  # noqa
    r"""Move files within workspace.
    ---
    put:
      summary: Move files within workspace.
      description: >-
        This resource moves files within the workspace. Resource is expecting
        a workflow UUID.
      operationId: move_files
      consumes:
        - application/json
      produces:
        - application/json
      parameters:
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: source
          in: query
          description: Required. Source file(s).
          required: true
          type: string
        - name: target
          in: query
          description: Required. Target file(s).
          required: true
          type: string
        - name: access_token
          in: query
          description: Required. The API access_token of workflow owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Message about successfully moved files is
            returned.
          schema:
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
          examples:
            application/json:
              {
                "message": "Files were successfully moved",
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
              }
        400:
          description: >-
            Request failed. The incoming payload seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. Either User or Workflow does not exist.
          examples:
            application/json:
              {
                "message": "Workflow 256b25f4-4cfb-4684-b7a8-73872ef455a1
                            does not exist"
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")
        source = request.args.get('source')
        target = request.args.get('target')
        response, http_response = current_rwc_api_client.api.\
            move_files(
                user=user_id,
                workflow_id_or_name=workflow_id_or_name,
                source=source,
                target=target).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/workflows/<workflow_id_or_name>/disk_usage',
                 methods=['GET'])
def get_workflow_disk_usage(workflow_id_or_name):  # noqa
    r"""Get workflow disk usage.

    ---
    get:
      summary: Get disk usage of a workflow.
      description: >-
        This resource reports the disk usage of a workflow.
        Resource is expecting a workflow UUID and some parameters .
      operationId: get_workflow_disk_usage
      produces:
        - application/json
      parameters:
        - name: access_token
          in: query
          description: Required. API access_token of workflow owner.
          required: true
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: parameters
          in: body
          description: >-
            Optional. Additional input parameters and operational options.
          required: false
          schema:
            type: object
      responses:
        200:
          description: >-
            Request succeeded. Info about the disk usage is
            returned.
          schema:
            type: object
            properties:
              workflow_id:
                type: string
              workflow_name:
                type: string
              user:
                type: string
              disk_usage_info:
                type: array
                items:
                  type: object
                  properties:
                    name:
                      type: string
                    size:
                      type: string
          examples:
            application/json:
              {
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest.1",
                "disk_usage_info": [{'name': 'file1.txt',
                                     'size': '12KB'},
                                    {'name': 'plot.png',
                                     'size': '100KB'}]
              }
        400:
          description: >-
            Request failed. The incoming data specification seems malformed.
          examples:
            application/json:
              {
                "message": "Malformed request."
              }
        403:
          description: >-
            Request failed. User is not allowed to access workflow.
          examples:
            application/json:
              {
                "message": "User 00000000-0000-0000-0000-000000000000
                            is not allowed to access workflow
                            256b25f4-4cfb-4684-b7a8-73872ef455a1"
              }
        404:
          description: >-
            Request failed. User does not exist.
          examples:
            application/json:
              {
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist"
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))
        parameters = request.json or {}

        if not workflow_id_or_name:
            raise ValueError("workflow_id_or_name is not supplied")
        workflow = _get_workflow_with_uuid_or_name(workflow_id_or_name,
                                                   user_id)
        summarize = bool(parameters.get('summarize', False))
        reana_fs = fs.open_fs(SHARED_VOLUME_PATH)
        if reana_fs.exists(workflow.get_workspace()):
            absolute_workspace_path = reana_fs.getospath(
                workflow.get_workspace())
            disk_usage_info = get_workspace_disk_usage(absolute_workspace_path,
                                                       summarize=summarize)
        else:
            raise ValueError('Workspace does not exist.')

        response = {'workflow_id': workflow.id_,
                    'workflow_name': workflow.name,
                    'user': user_id,
                    'disk_usage_info': disk_usage_info}

        return jsonify(response), 200
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except ValueError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 403
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
