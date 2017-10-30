# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017 CERN.
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

"""Reana-Server Ping-functionality Flask-Blueprint."""

from flask import current_app as app
from flask import Blueprint, jsonify, request

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
      produces:
       - application/json
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
                  "organization": "default_org",
                  "status": "running",
                  "user": "00000000-0000-0000-0000-000000000000"
                },
                {
                  "id": "3c9b117c-d40a-49e3-a6de-5f89fcada5a3",
                  "organization": "default_org",
                  "status": "finished",
                  "user": "00000000-0000-0000-0000-000000000000"
                },
                {
                  "id": "72e3ee4f-9cd3-4dc7-906c-24511d9f5ee3",
                  "organization": "default_org",
                  "status": "waiting",
                  "user": "00000000-0000-0000-0000-000000000000"
                },
                {
                  "id": "c4c0a1a6-beef-46c7-be04-bf4b3beca5a1",
                  "organization": "default_org",
                  "status": "waiting",
                  "user": "00000000-0000-0000-0000-000000000000"
                }
              ]
        500:
          description: >-
            Request failed. Internal controller error.
          examples:
            application/json:
              {
                "message": "Either organization or user doesn't exist."
              }
    """
    try:
        response, status_code = rwc_api_client.api.get_workflows(
            organization='default',
            user='00000000-0000-0000-0000-000000000000').result()
        return jsonify(response), status_code
    except Exception as e:
        return jsonify({"message": str(e)}), 500


@blueprint.route('/analyses', methods=['POST'])
def create_analysis():  # noqa
    r"""Create a analysis.

    ---
    post:
      summary: Creates a new yadage workflow.
      description: >-
        This resource is expecting JSON data with all the necessary
        information to instantiate a yadage workflow.
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
        200:
          description: >-
            Request succeeded. The workflow has been instantiated.
          schema:
            type: object
            properties:
              message:
                type: string
              workflow_id:
                type: string
          examples:
            application/json:
              {
                "message": "Analysis successfully launched",
                "workflow_id": "cdcf48b1-c2f3-4693-8230-b066e088c6ac"
              }
        400:
          description: >-
            Request failed. The incoming data specification seems malformed
    """
    try:
        reana_spec_file = None
        reana_spec_url = None
        if request.json:
            # validate against schema
            reana_spec = request.json
            workflow_engine = reana_spec['workflow']['type']
        elif request.args.get('spec'):
            # TODO implement load workflow engine from remote
            return jsonify('Not implemented'), 501
        else:
            raise Exception('Either remote repository or a reana spec need to \
            be provided')

        if workflow_engine not in app.config['AVAILABLE_WORKFLOW_ENGINES']:
            raise Exception('Unknown workflow engine')

        if workflow_engine == 'yadage':
            if reana_spec_file:
                # From spec file
                (response, status_code) = \
                    rwc_api_client.api.run_yadage_workflow_from_spec(
                        workflow={
                            'parameters': reana_spec['parameters'],
                            'workflow_spec': reana_spec['workflow']['spec'],
                        },
                        user=request.args.get('user'),
                        organization=request.args.get('organization')).result()

        elif workflow_engine == 'cwl':
            # From spec file
            (response, status_code) = rwc_api_client.api.run_cwl_workflow_from_spec(
                workflow={
                    'parameters': reana_spec['parameters']['input'],
                    'workflow_spec': reana_spec['workflow']['spec'],
                },
                user=request.args.get('user'),
                organization=request.args.get('organization')).result()

        return jsonify(response), status_code

    except KeyError as e:
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        return jsonify({"message": str(e)}), 500


@blueprint.route('/analyses/<analysis_id>/workspace', methods=['POST'])
def seed_analysis(analysis_id):  # noqa
    r"""Seed analysis with files.

    ---
    post:
      summary: Seeds the analysis workspace with the provided file.
      description: >-
        This resource expects a file which will be placed in the analysis
        workspace identified by the UUID `analysis_id`.
      operationId: seed_analysis
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
        - name: analysis_id
          in: path
          description: Required. Analysis UUID.
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
            Request failed. The incoming data specification seems malformed
    """
    try:
        file_ = request.files['file_content'].stream.read()
        response, status_code = rwc_api_client.api.seed_workflow(
            user=request.args['user'],
            organization=request.args['organization'],
            workflow_id=analysis_id,
            file_content=file_,
            file_name=request.args['file_name']).result()

        return jsonify(response), status_code
    except KeyError as e:
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        return jsonify({"message": str(e)}), 500
