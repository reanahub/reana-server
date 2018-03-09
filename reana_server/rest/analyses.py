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

"""Reana-Server analysis-functionality Flask-Blueprint."""
import io
import logging
import traceback

from bravado.exception import HTTPError
from flask import current_app as app
from flask import Blueprint, jsonify, request, send_file

from reana_server.utils import is_uuid_v4
from ..api_client import create_openapi_client

blueprint = Blueprint('analyses', __name__)
rwc_api_client = create_openapi_client('reana-workflow-controller')


@blueprint.route('/analyses', methods=['GET'])
def get_analyses():  # noqa
    r"""Get all current analyses in REANA.

    ---
    get:
      summary: Returns list of all current analyses in REANA.
      description: >-
        This resource return all current analyses in JSON format.
      operationId: get_analyses
      produces:
       - application/json
      parameters:
        - name: organization
          in: query
          description: Required. Organization which the analysis belongs to.
          required: true
          type: string
        - name: user
          in: query
          description: Required. UUID of analysis owner.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. The response contains the list of all analyses.
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
        response, http_response = rwc_api_client.api.get_workflows(
            user=request.args.get('user'),
            organization=request.args.get('organization')).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/analyses', methods=['POST'])
def create_analysis():  # noqa
    r"""Create a analysis.

    ---
    post:
      summary: Creates a new workflow based on a REANA specification file.
      description: >-
        This resource is expecting a REANA specification in JSON format with
        all the necessary information to instantiate a workflow.
      operationId: create_analysis
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
        - name: user
          in: query
          description: Required. UUID of workflow owner.
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
            an analysis.
          required: false
          schema:
            type: object
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
            user=request.args.get('user'),
            organization=request.args.get('organization')).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/analyses/<analysis_id_or_name>/workspace/inputs',
                 methods=['POST'])
def seed_analysis_input(analysis_id_or_name):  # noqa
    r"""Seed analysis with input files.

    ---
    post:
      summary: Seeds the analysis workspace with the provided file.
      description: >-
        This resource expects a file which will be placed in the analysis
        workspace identified by the UUID `analysis_id`.
      operationId: seed_analysis_inputs
      consumes:
        - multipart/form-data
      produces:
        - application/json
      parameters:
        - name: organization
          in: query
          description: Required. Organization which the analysis belongs to.
          required: true
          type: string
        - name: user
          in: query
          description: Required. UUID of analysis owner.
          required: true
          type: string
        - name: analysis_id_or_name
          in: path
          description: Required. Analysis UUID or name
          required: true
          type: string
        - name: file_content
          in: formData
          description: >-
            Required. File to be transferred to the analysis workspace.
          required: true
          type: file
        - name: file_name
          in: query
          description: Required. File name.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. File successfully trasferred.
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
        workflow_id_or_name = analysis_id_or_name

        if not workflow_id_or_name:
            raise KeyError("analysis_id_or_name is not supplied")

        file_ = request.files['file_content'].stream.read()
        response, http_response = rwc_api_client.api.seed_workflow_files(
            user=request.args['user'],
            organization=request.args['organization'],
            workflow_id_or_name=analysis_id_or_name,
            file_content=file_,
            file_name=request.args['file_name'],
            file_type='input').result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/analyses/<analysis_id_or_name>/workspace/code',
                 methods=['POST'])
def seed_analysis_code(analysis_id_or_name):  # noqa
    r"""Seed analysis with code files.

    ---
    post:
      summary: Seeds the analysis workspace with the provided file.
      description: >-
        This resource expects a file which will be placed in the analysis
        workspace identified by the UUID `analysis_id`.
      operationId: seed_analysis_code
      consumes:
        - multipart/form-data
      produces:
        - application/json
      parameters:
        - name: organization
          in: query
          description: Required. Organization which the analysis belongs to.
          required: true
          type: string
        - name: user
          in: query
          description: Required. UUID of analysis owner.
          required: true
          type: string
        - name: analysis_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: file_content
          in: formData
          description: >-
            Required. File to be transferred to the analysis workspace.
          required: true
          type: file
        - name: file_name
          in: query
          description: Required. File name.
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
        workflow_id_or_name = analysis_id_or_name

        if not workflow_id_or_name:
            raise KeyError("analysis_id_or_name is not supplied")

        file_ = request.files['file_content'].stream.read()
        response, http_response = rwc_api_client.api.seed_workflow_files(
            user=request.args['user'],
            organization=request.args['organization'],
            workflow_id_or_name=analysis_id_or_name,
            file_content=file_,
            file_name=request.args['file_name'],
            file_type='code').result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/analyses/<analysis_id_or_name>/logs', methods=['GET'])
def get_analysis_logs(analysis_id_or_name):  # noqa
    r"""Get analysis logs.

    ---
    get:
      summary: Get workflow logs of an analysis.
      description: >-
        This resource reports the status of an analysis.
        Resource is expecting a analysis UUID.
      operationId: get_analysis_logs
      produces:
        - application/json
      parameters:
        - name: organization
          in: query
          description: Required. Organization which the worklow belongs to.
          required: true
          type: string
        - name: user
          in: query
          description: Required. UUID of workflow owner.
          required: true
          type: string
        - name: analysis_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Info about an analysis, including the status is
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
        user = request.args['user']
        organization = request.args['organization']
        workflow_id_or_name = analysis_id_or_name

        if not workflow_id_or_name:
            raise KeyError("analysis_id_or_name is not supplied")

        response, http_response = rwc_api_client.api.get_workflow_logs(
            user=user,
            organization=organization,
            workflow_id_or_name=analysis_id_or_name).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/analyses/<analysis_id_or_name>/status', methods=['GET'])
def analysis_status(analysis_id_or_name):  # noqa
    r"""Get analysis status.

    ---
    get:
      summary: Get status of an analysis.
      description: >-
        This resource reports the status of an analysis.
        Resource is expecting a analysis UUID.
      operationId: get_analysis_status
      produces:
        - application/json
      parameters:
        - name: organization
          in: query
          description: Required. Organization which the worklow belongs to.
          required: true
          type: string
        - name: user
          in: query
          description: Required. UUID of workflow owner.
          required: true
          type: string
        - name: analysis_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
      responses:
        200:
          description: >-
            Request succeeded. Info about an analysis, including the status is
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
              status:
                type: string
              user:
                type: string
          examples:
            application/json:
              {
                "id": "256b25f4-4cfb-4684-b7a8-73872ef455a1",
                "name": "mytest-1",
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
        user = request.args['user']
        organization = request.args['organization']
        workflow_id_or_name = analysis_id_or_name

        if not workflow_id_or_name:
            raise KeyError("analysis_id_or_name is not supplied")

        response, http_response = rwc_api_client.api.get_workflow_status(
            user=user,
            organization=organization,
            workflow_id_or_name=workflow_id_or_name).result()

        return jsonify(response), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/analyses/<analysis_id_or_name>/status', methods=['PUT'])
def set_analysis_status(analysis_id_or_name):  # noqa
    r"""Set analysis status.
    ---
    put:
      summary: Set status of an analysis.
      description: >-
        This resource reports the status of an analysis.
        Resource is expecting a analysis UUID.
      operationId: set_analysis_status
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
        - name: user
          in: query
          description: Required. UUID of workflow owner.
          required: true
          type: string
        - name: analysis_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
        - name: status
          in: body
          description: Required. New analysis status.
          required: true
          schema:
            type: string
            description: Required. New status.
      responses:
        200:
          description: >-
            Request succeeded. Info about an analysis, including the status is
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
        user = request.args['user']
        organization = request.args['organization']
        workflow_id_or_name = analysis_id_or_name

        if not workflow_id_or_name:
            raise KeyError("analysis_id_or_name is not supplied")

        status = request.json

        response, http_response = rwc_api_client.api.set_workflow_status(
            user=user,
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
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route(
    '/analyses/<analysis_id_or_name>/workspace/outputs/<path:file_name>',
    methods=['GET'])
def get_analysis_outputs_file(analysis_id_or_name, file_name):  # noqa
    r"""Get analysis status.

    ---
    get:
      summary: Returns the requested file.
      description: >-
        This resource is expecting a workflow UUID and a file name to return
        its content.
      operationId: get_analysis_outputs_file
      produces:
        - multipart/form-data
      parameters:
        - name: organization
          in: query
          description: Required. Organization which the analysis belongs to.
          required: true
          type: string
        - name: user
          in: query
          description: Required. UUID of analysis owner.
          required: true
          type: string
        - name: analysis_id_or_name
          in: path
          description: Required. analysis UUID or name.
          required: true
          type: string
        - name: file_name
          in: path
          description: Required. Name (or path) of the file to be downloaded.
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
        user = request.args['user'],
        organization = request.args['organization'],
        workflow_id_or_name = analysis_id_or_name

        if not workflow_id_or_name:
            raise KeyError("analysis_id_or_name is not supplied")

        response, http_response = rwc_api_client.api.get_workflow_outputs_file(
            user=user,
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
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), 500


@blueprint.route('/analyses/<analysis_id_or_name>/workspace/inputs/',
                 methods=['GET'])
def get_analysis_inputs_list(analysis_id_or_name):  # noqa
    r"""List all analysis input files.

    ---
    get:
      summary: Returns the list of input files for a specific analysis.
      description: >-
        This resource is expecting an analysis UUID to return its list of
        input files.
      operationId: get_analysis_inputs
      produces:
       - application/json
      parameters:
        - name: organization
          in: query
          description: Required. Organization which the analysis belongs to.
          required: true
          type: string
        - name: user
          in: query
          description: Required. UUID of analysis owner.
          required: true
          type: string
        - name: analysis_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
      responses:
        200:
          description: >-
            Requests succeeded. The list of input files has been retrieved.
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
        workflow_id_or_name = analysis_id_or_name

        if not workflow_id_or_name:
            raise KeyError("analysis_id_or_name is not supplied")

        response, http_response = rwc_api_client.api.get_workflow_files(
            user=request.args.get('user'),
            organization=request.args.get('organization'),
            workflow_id_or_name=analysis_id_or_name,
            file_type='input').result()

        return jsonify(http_response.json()), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/analyses/<analysis_id_or_name>/workspace/code/',
                 methods=['GET'])
def get_analysis_code_list(analysis_id_or_name):  # noqa
    r"""List all code files for a given analysis.

    ---
    get:
      summary: Returns the list of code files for a specific analysis.
      description: >-
        This resource is expecting an analysis UUID to return its list of
        code files.
      operationId: get_analysis_code
      produces:
       - application/json
      parameters:
        - name: organization
          in: query
          description: Required. Organization which the analysis belongs to.
          required: true
          type: string
        - name: user
          in: query
          description: Required. UUID of analysis owner.
          required: true
          type: string
        - name: analysis_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
      responses:
        200:
          description: >-
            Requests succeeded. The list of code files has been retrieved.
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
        workflow_id_or_name = analysis_id_or_name

        if not workflow_id_or_name:
            raise KeyError("analysis_id_or_name is not supplied")

        response, http_response = rwc_api_client.api.get_workflow_files(
            user=request.args.get('user'),
            organization=request.args.get('organization'),
            workflow_id_or_name=analysis_id_or_name,
            file_type='code').result()

        return jsonify(http_response.json()), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500


@blueprint.route('/analyses/<analysis_id_or_name>/workspace/outputs/',
                 methods=['GET'])
def get_analysis_outputs_list(analysis_id_or_name):  # noqa
    r"""List all analysis output files.

    ---
    get:
      summary: Returns the list of output files for a specific analysis.
      description: >-
        This resource is expecting an analysis UUID to return its list of
        output files.
      operationId: get_analysis_outputs
      produces:
       - application/json
      parameters:
        - name: organization
          in: query
          description: Required. Organization which the analysis belongs to.
          required: true
          type: string
        - name: user
          in: query
          description: Required. UUID of analysis owner.
          required: true
          type: string
        - name: analysis_id_or_name
          in: path
          description: Required. Analysis UUID or name.
          required: true
          type: string
      responses:
        200:
          description: >-
            Requests succeeded. The list of output files has been retrieved.
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
        workflow_id_or_name = analysis_id_or_name

        if not workflow_id_or_name:
            raise KeyError("analysis_id_or_name is not supplied")

        response, http_response = rwc_api_client.api.get_workflow_files(
            user=request.args.get('user'),
            organization=request.args.get('organization'),
            workflow_id_or_name=analysis_id_or_name,
            file_type='output').result()

        return jsonify(http_response.json()), http_response.status_code
    except HTTPError as e:
        logging.error(traceback.format_exc())
        return jsonify(e.response.json()), e.response.status_code
    except KeyError as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"message": str(e)}), 500
