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

RUN apt-get update && \
    apt-get install -y vim-tiny

RUN pip install -e git://github.com/reanahub/reana-commons.git@master#egg=reana-commons

ADD . /code
WORKDIR /code

# Debug off by default
ARG DEBUG=false

RUN if [ "${DEBUG}" = "true" ]; then pip install -r requirements-dev.txt; pip install -e .[all]; else pip install .[all]; fi;

ARG UWSGI_PROCESSES=2
ENV UWSGI_PROCESSES ${UWSGI_PROCESSES:-2}
ARG UWSGI_THREADS=2
ENV UWSGI_THREADS ${UWSGI_THREADS:-2}
ENV TERM=xterm
ENV FLASK_APP=/code/reana_server/app.py

EXPOSE 5000

CMD flask db init && \
    flask users create info@reana.io &&\
    uwsgi --module reana_server.app:app \
    --http-socket 0.0.0.0:5000 --master \
    --processes ${UWSGI_PROCESSES} --threads ${UWSGI_THREADS} \
    --stats /tmp/stats.socket \
    --wsgi-disable-file-wrapper
