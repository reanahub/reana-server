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

"""Reana-Server."""

import logging

from flask import Flask, Response, jsonify

app = Flask(__name__)
app.secret_key = "hyper secret key"


@app.route('/api/ping', methods=['GET'])
def ping():  # noqa
    r"""Endpoint to ping the server. Responds with a pong.

    ---
    get:
      summary: Ping the server (healthcheck)
      description: >-
        Ping the server.
      produces:
       - application/json
      responses:
        200:
          description: >-
            Ping succeeded. Service is running and accessible.
          examples:
            application/json:
              message: OK
              status: 200
    """

    return jsonify(message="OK", status="200"), 200


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(threadName)s - %(levelname)s: %(message)s'
    )

    app.config.from_object('config')

    app.run(debug=True, port=5000,
            host='0.0.0.0')
