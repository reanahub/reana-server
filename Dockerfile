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
# You should have received a copy of the GNU General Public License along with
# REANA; if not, write to the Free Software Foundation, Inc., 59 Temple Place,
# Suite 330, Boston, MA 02111-1307, USA.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization or
# submit itself to any jurisdiction.

FROM python:3.6

ENV TERM=xterm
RUN apt-get update && \
    apt-get install -y vim-tiny

ADD . /code
WORKDIR /code

# Debug off by default
ARG DEBUG=false

RUN if [ "${DEBUG}" = "true" ]; then pip install -r requirements-dev.txt; pip install -e .[all]; else pip install .[all]; fi;

RUN adduser --uid 1000 --disabled-password --gecos '' reanauser && \
    chown -R reanauser:reanauser /code
USER reanauser
EXPOSE 5000
ENV FLASK_APP=/code/reana_server/app.py
CMD ["flask", "run", "--host=0.0.0.0"]
