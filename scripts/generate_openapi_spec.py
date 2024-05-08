#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2020, 2021, 2022, 2023 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""OpenAPI specification file generator script."""

import json
import os

import click
from apispec import APISpec
from apispec_webframeworks.flask import FlaskPlugin
from flask import current_app
from reana_commons.utils import copy_openapi_specs
from swagger_spec_validator.validator20 import validate_json

from reana_server.version import __version__
from reana_server.factory import create_minimal_app

# Import your marshmallow schemas here
# from example_package.schemas import Example_schema,

# Software title. E.g. just name of the module exposing the API.
__title__ = "REANA Server"

# Short description of the API. Supports GitHub Flavored Markdown.
__api_description__ = "Submit workflows to be run on REANA Cloud"

# Filepath where the OpenAPI specification file should be written to.
__output_path__ = "temp_openapi.json"


@click.command()
@click.option(
    "-p",
    "--publish",
    help="Optional parameters if set, will pass generated and "
    "validated openapi specifications to reana-commons module. "
    "E.g. --publish.",
    count=True,
)
def build_openapi_spec(publish):
    """Creates an OpenAPI definition of Flask application,
    check conformity of generated definition against OpenAPI 2.0 specification
    and writes it into a file."""

    package = __title__
    desc = __api_description__
    ver = __version__

    # Create OpenAPI specification object
    spec = APISpec(
        title=package,
        version=ver,
        openapi_version="2.0",
        info=dict(description=desc),
        plugins=(FlaskPlugin(),),
    )

    # Add marshmallow schemas to the specification here
    # spec.definition('Example', schema=Example_schema)

    # Collect OpenAPI docstrings from Flask endpoints
    for key in current_app.view_functions:
        if key != "static" and key != "get_openapi_spec":
            spec.path(view=current_app.view_functions[key])

    spec_json = json.dumps(
        spec.to_dict(), indent=2, separators=(",", ": "), sort_keys=True
    )

    # Output spec to JSON file
    with click.open_file(
        __output_path__,
        mode="w+",
        encoding=None,
        errors="strict",
        lazy=False,
        atomic=False,
    ) as output_file:
        output_file.write(spec_json + "\n")

        click.echo(
            click.style(
                "OpenAPI specification written to {}".format(output_file.name),
                fg="green",
            )
        )

    # Check that generated spec passes validation. Done after writing to file
    # in order to give user easy way to access the possible erroneous spec.
    with open(os.path.join(os.getcwd(), __output_path__)) as output_file:
        validate_json(json.load(output_file), "schemas/v2.0/schema.json")

        click.echo(
            click.style("OpenAPI specification validated successfully", fg="green")
        )
        if publish:
            copy_openapi_specs(__output_path__, "reana-server")

    return spec.to_dict()


if __name__ == "__main__":
    app = create_minimal_app()
    with app.app_context():
        build_openapi_spec()
