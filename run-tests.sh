#!/bin/bash
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2020, 2021, 2022, 2023 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

# Quit on errors
set -o errexit

# Quit on unbound symbols
set -o nounset

export REANA_SQLALCHEMY_DATABASE_URI=postgresql+psycopg2://postgres:mysecretpassword@localhost/postgres

# Verify that db container is running before continuing
_check_ready () {
    RETRIES=40
    while ! $2
    do
        echo "==> [INFO] Waiting for $1, $((RETRIES--)) remaining attempts..."
        sleep 2
        if [ $RETRIES -eq 0 ]
        then
            echo "==> [ERROR] Couldn't reach $1"
            exit 1
        fi
    done
}

_db_check () {
    docker exec --user postgres postgres__reana-server bash -c "pg_isready" &>/dev/null;
}

clean_old_db_container () {
    OLD="$(docker ps --all --quiet --filter=name=postgres__reana-server)"
    if [ -n "$OLD" ]; then
        echo '==> [INFO] Cleaning old DB container...'
        docker stop postgres__reana-server
    fi
}

start_db_container () {
    echo '==> [INFO] Starting DB container...'
    docker run --rm --name postgres__reana-server -p 5432:5432 -e POSTGRES_PASSWORD=mysecretpassword -d docker.io/library/postgres:12.13
    _check_ready "Postgres" _db_check
}

stop_db_container () {
    echo '==> [INFO] Stopping DB container...'
    docker stop postgres__reana-server
}

check_script () {
    shellcheck run-tests.sh
}

check_pydocstyle () {
    pydocstyle reana_server
}

check_black () {
    black --check .
}

check_flake8 () {
    flake8 .
}

check_openapi_spec () {
    FLASK_APP=reana_server/app.py python ./scripts/generate_openapi_spec.py
    diff -q -w temp_openapi.json docs/openapi.json
    rm temp_openapi.json
}

check_manifest () {
    check-manifest
}

check_sphinx () {
    sphinx-build -qnNW docs docs/_build/html
}

check_pytest () {
    clean_old_db_container
    start_db_container
    python setup.py test
    stop_db_container
}

check_dockerfile () {
    docker run -i --rm docker.io/hadolint/hadolint:v2.12.0 < Dockerfile
}

check_docker_build () {
    docker build -t docker.io/reanahub/reana-server .
}

check_all () {
    check_script
    check_pydocstyle
    check_black
    check_flake8
    check_openapi_spec
    check_manifest
    check_sphinx
    check_pytest
    check_dockerfile
    check_docker_build
}

if [ $# -eq 0 ]; then
    check_all
    exit 0
fi

for arg in "$@"
do
    case $arg in
        --check-shellscript) check_script;;
        --check-pydocstyle) check_pydocstyle;;
        --check-black) check_black;;
        --check-flake8) check_flake8;;
        --check-openapi-spec) check_openapi_spec;;
        --check-manifest) check_manifest;;
        --check-sphinx) check_sphinx;;
        --check-pytest) check_pytest;;
        --check-dockerfile) check_dockerfile;;
        --check-docker-build) check_docker_build;;
        *)
    esac
done
