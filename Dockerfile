# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

# Install base image and its dependencies
FROM python:3.8-slim
# hadolint ignore=DL3008, DL3009, DL3015
RUN apt-get update && \
    apt-get install -y \
      gcc \
      vim-tiny \
      libffi-dev \
      procps

# Install dependencies
COPY requirements.txt /code/
RUN pip install -r /code/requirements.txt

# Copy cluster component source code
WORKDIR /code
COPY . /code

# Are we debugging?
ARG DEBUG=0
RUN if [ "${DEBUG}" -gt 0 ]; then pip install -e ".[debug]"; else pip install .; fi;

# Are we building with locally-checked-out shared modules?
# hadolint ignore=SC2102
RUN if test -e modules/reana-commons; then pip install -e modules/reana-commons[kubernetes] --upgrade; fi
RUN if test -e modules/reana-db; then pip install -e modules/reana-db --upgrade; fi

# Check if there are broken requirements
RUN pip check

# Set useful environment variables
ENV TERM=xterm \
    FLASK_APP=/code/reana_server/app.py

# Expose ports to clients
EXPOSE 5000

# Run server
CMD ["uwsgi --ini uwsgi.ini"]
