# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

FROM python:3.6-slim

RUN apt-get update && \
    apt-get install -y \
      gcc \
      vim-tiny \
      libffi-dev \
      procps && \
    pip install --upgrade pip

COPY CHANGES.rst README.rst setup.py /code/
COPY reana_server/version.py /code/reana_server/
WORKDIR /code
RUN pip install --no-cache-dir requirements-builder && \
    requirements-builder -l pypi setup.py | pip install --no-cache-dir -r /dev/stdin && \
    pip uninstall -y requirements-builder

COPY . /code

# Debug off by default
ARG DEBUG=0
RUN if [ "${DEBUG}" -gt 0 ]; then pip install -r requirements-dev.txt; pip install -e .; else pip install .; fi;

# Building with locally-checked-out shared modules?
RUN if test -e modules/reana-commons; then pip install -e modules/reana-commons[kubernetes] --upgrade; fi
RUN if test -e modules/reana-db; then pip install -e modules/reana-db --upgrade; fi

# Check if there are broken requirements
RUN pip check

ENV TERM=xterm
ENV FLASK_APP=/code/reana_server/app.py

EXPOSE 5000

CMD uwsgi --ini uwsgi.ini
