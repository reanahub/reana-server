#!/bin/sh
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

pydocstyle reana_server && \
isort -rc -c -df **/*.py && \
FLASK_APP=reana_server/app.py python ./scripts/generate_openapi_spec.py && \
diff -q -w temp_openapi.json docs/openapi.json && \
check-manifest --ignore ".travis-*" && \
sphinx-build -qnN docs docs/_build/ && \
python setup.py test && \
sphinx-build -qnN -b doctest docs docs/_build/doctest && \
docker build -t reanahub/reana-server .
