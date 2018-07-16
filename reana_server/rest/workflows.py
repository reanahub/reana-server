# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# REANA is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with REANA; if not, see <http://www.gnu.org/licenses>.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization or
# submit itself to any jurisdiction.

"""Reana-Server workflow-functionality Flask-Blueprint."""
import io
import logging
import traceback

from bravado.exception import HTTPError
from flask import current_app as app
from flask import Blueprint, jsonify, request, send_file

from reana_server.utils import is_uuid_v4, get_user_from_token
from ..api_client import create_openapi_client

blueprint = Blueprint('workflows', __name__)
rwc_api_client = create_openapi_client('reana-workflow-controller')


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
        - name: organization
          in: query
          description: Required. Organization which the workflow belongs to.
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
                organization:
                  type: string
                status:
                  type: string
                user:
                  type: string
          examples:
            application/json:
              [
                {
                  "id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                  "name": "mytest-1",
                  "organization": "default_org",
                  "status": "running",
                  "user": "00000000-0000-0000-0000-000000000000"
                },
                {
                  "id": "3c9b117c-d40a-49e3-a6de-5f89fcada5a3",
                  "name": "mytest-2",
                  "organization": "default_org",
                  "status": "finished",
                  "user": "00000000-0000-0000-0000-000000000000"
                },
                {
                  "id": "72e3ee4f-9cd3-4dc7-906c-24511d9f5ee3",
                  "name": "mytest-3",
                  "organization": "default_org",
                  "status": "waiting",
                  "user": "00000000-0000-0000-0000-000000000000"
                },
                {
                  "id": "c4c0a1a6-beef-46c7-be04-bf4b3beca5a1",
                  "name": "mytest-4",
                  "organization": "default_org",
                  "status": "waiting",
                  "user": "00000000-0000-0000-0000-000000000000"
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
                "message": "Either organization or user does not exist."
              }
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))
        response, http_response = rwc_api_client.api.get_workflows(
            user=user_id,
            organization=request.args.get('organization')).result()

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
        - name: organization
          in: query
          description: Required. Organization which the worklow belongs to.
          required: true
          type: string
        - name: workflow_name
          in: query
          description: Name of the workflow to be created. If not provided
            name will be generated.
          required: true
          type: string
        - name: spec
          in: query
          description: Remote repository which contains a valid REANA
            specification.
          required: false
          type: string
        - name: reana_spec
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
                "workflow_name": "mytest-1"
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

        response, http_response = rwc_api_client.api.create_workflow(
            workflow={
                'parameters': reana_spec_file['inputs']['parameters'],
                'specification': reana_spec_file['workflow']['spec'],
                'type': reana_spec_file['workflow']['type'],
                'name': workflow_name
            },
            user=user_id,
            organization=request.args.get('organization')).result()

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
        - name: organization
          in: query
          description: Required. Organization which the worklow belongs to.
          required: true
          type: string
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
              organization:
                type: string
              logs:
                type: string
              user:
                type: string
          examples:
            application/json:
              {
                "workflow_id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "workflow_name": "mytest-1",
                "organization": "default_org",
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
        organization = request.args['organization']
        workflow_id_or_name = workflow_id_or_name

        if not workflow_id_or_name:
            raise KeyError("workflow_id_or_name is not supplied")

        response, http_response = rwc_api_client.api.get_workflow_logs(
            user=user_id,
            organization=organization,
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


@blueprint.route('/workflows/<workflow_id_or_name>/status', methods=['GET'])
def workflow_status(workflow_id_or_name):  # noqa
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
        - name: organization
          in: query
          description: Required. Organization which the worklow belongs to.
          required: true
          type: string
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
              organization:
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
                "id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "name": "mytest-1",
                "organization": "default_org",
                "created": "2018-06-13 09:47:35.660977",
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
        organization = request.args['organization']
        workflow_id_or_name = workflow_id_or_name

        if not workflow_id_or_name:
            raise KeyError("workflow_id_or_name is not supplied")

        response, http_response = rwc_api_client.api.get_workflow_status(
            user=user_id,
            organization=organization,
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
        - name: organization
          in: query
          description: Required. Organization which the worklow belongs to.
          required: true
          type: string
        - name: workflow_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: status
          in: body
          description: Required. New workflow status.
          required: true
          schema:
            type: string
            description: Required. New status.
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
              message:
                type: string
              workflow_id:
                type: string
              workflow_name:
                type: string
              organization:
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
                "workflow_name": "mytest-1",
                "organization": "default_org",
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
        organization = request.args['organization']
        workflow_id_or_name = workflow_id_or_name

        if not workflow_id_or_name:
            raise KeyError("workflow_id_or_name is not supplied")

        status = request.json

        response, http_response = rwc_api_client.api.set_workflow_status(
            user=user_id,
            organization=organization,
            workflow_id_or_name=workflow_id_or_name,
            status=status).result()

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
        - name: organization
          in: query
          description: Required. Organization which the workflow belongs to.
          required: true
          type: string
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
          examples:
            application/json:
              {
                "message": "File successfully transferred",
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
                "message": "Workflow cdcf48b1-c2f3-4693-8230-b066e088c6ac does
                            not exist"
              }
        500:
          description: >-
            Request failed. Internal controller error.
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))
        workflow_id_or_name = workflow_id_or_name

        if not workflow_id_or_name:
            raise KeyError("workflow_id_or_name is not supplied")

        file_ = request.files['file_content'].stream.read()
        response, http_response = rwc_api_client.api.upload_file(
            user=user_id,
            organization=request.args['organization'],
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
        - name: organization
          in: query
          description: Required. Organization which the workflow belongs to.
          required: true
          type: string
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
            Request failed. Internal controller error.
          examples:
            application/json:
              {
                "message": "Either organization or user does not exist."
              }
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))
        organization = request.args['organization']
        workflow_id_or_name = workflow_id_or_name

        if not workflow_id_or_name:
            raise KeyError("workflow_id_or_name is not supplied")

        response, http_response = rwc_api_client.api.download_file(
            user=user_id,
            organization=organization,
            workflow_id_or_name=workflow_id_or_name,
            file_name=file_name).result()

        return send_file(
            io.BytesIO(http_response.raw_bytes),
            attachment_filename=file_name,
            mimetype='multipart/form-data'), 200
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
        - name: organization
          in: query
          description: Required. Organization which the workflow belongs to.
          required: true
          type: string
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
                  format: date-time
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
            Request failed. Internal controller error.
          examples:
            application/json:
              {
                "message": "Either organization or user does not exist."
              }
    """
    try:
        user_id = get_user_from_token(request.args.get('access_token'))
        workflow_id_or_name = workflow_id_or_name

        if not workflow_id_or_name:
            raise KeyError("workflow_id_or_name is not supplied")

        response, http_response = rwc_api_client.api.get_files(
            user=user_id,
            organization=request.args.get('organization'),
            workflow_id_or_name=workflow_id_or_name).result()

        return jsonify(http_response.json()), http_response.status_code
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
