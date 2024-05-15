# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021, 2022, 2023 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

# Use Ubuntu LTS base image
FROM docker.io/library/ubuntu:20.04

# Use default answers in installation commands
ENV DEBIAN_FRONTEND=noninteractive

# Use distutils provided by the standard Python library instead of the vendored one in
# setuptools, so that editable installations are stored in the right directory.
# See https://github.com/pypa/setuptools/issues/3301
ENV SETUPTOOLS_USE_DISTUTILS=stdlib

# Prepare list of Python dependencies
COPY requirements.txt /code/

# Install all system and Python dependencies in one go
# hadolint ignore=DL3008,DL3013
RUN apt-get update -y && \
    apt-get install --no-install-recommends -y \
      gcc \
      git \
      libffi-dev \
      libpcre3 \
      libpcre3-dev \
      libpython3.8 \
      procps \
      python3-pip \
      python3.8 \
      python3.8-dev \
      vim-tiny && \
    pip install --no-cache-dir --upgrade pip setuptools && \
    pip install --no-cache-dir -r /code/requirements.txt && \
    apt-get remove -y \
      gcc \
      python3.8-dev && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy cluster component source code
WORKDIR /code
COPY . /code

# Are we debugging?
ARG DEBUG=0
RUN if [ "${DEBUG}" -gt 0 ]; then pip install --no-cache-dir -e ".[debug]"; else pip install --no-cache-dir .; fi;

# Are we building with locally-checked-out shared modules?
# hadolint ignore=DL3013
RUN if test -e modules/reana-commons; then \
      if [ "${DEBUG}" -gt 0 ]; then \
        pip install --no-cache-dir -e "modules/reana-commons[kubernetes,yadage,snakemake,cwl]" --upgrade; \
      else \
        pip install --no-cache-dir "modules/reana-commons[kubernetes,yadage,snakemake,cwl]" --upgrade; \
      fi \
    fi; \
    if test -e modules/reana-db; then \
      if [ "${DEBUG}" -gt 0 ]; then \
        pip install --no-cache-dir -e "modules/reana-db" --upgrade; \
      else \
        pip install --no-cache-dir "modules/reana-db" --upgrade; \
      fi \
    fi

# A quick fix to allow eduGAIN and social login users that wouldn't otherwise match Invenio username rules
RUN sed -i 's|^username_regex = re.compile\(.*\)$|username_regex = re.compile("^\\S+$")|g' /usr/local/lib/python3.8/dist-packages/invenio_userprofiles/validators.py

# Check for any broken Python dependencies
# hadolint ignore=DL3059
RUN pip check

# Set useful environment variables
ENV TERM=xterm \
    FLASK_APP=/code/reana_server/app.py

# Expose ports to clients
EXPOSE 5000

# Run server
CMD ["uwsgi --ini uwsgi.ini"]

# Set image labels
LABEL org.opencontainers.image.authors="team@reanahub.io"
LABEL org.opencontainers.image.created="2024-03-04"
LABEL org.opencontainers.image.description="REANA reproducible analysis platform - server component"
LABEL org.opencontainers.image.documentation="https://reana-server.readthedocs.io/"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/reanahub/reana-server"
LABEL org.opencontainers.image.title="reana-server"
LABEL org.opencontainers.image.url="https://github.com/reanahub/reana-server"
LABEL org.opencontainers.image.vendor="reanahub"
# x-release-please-start-version
LABEL org.opencontainers.image.version="0.9.4"
# x-release-please-end
