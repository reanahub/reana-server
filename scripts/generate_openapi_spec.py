#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""OpenAPI specification file generator script."""

import json
import os
import shutil

import click
from apispec import APISpec
from flask import current_app
from flask.cli import with_appcontext
from swagger_spec_validator.validator20 import validate_json

# Import your marshmallow schemas here
# from example_package.schemas import Example_schema,

# Software title. E.g. just name of the module exposing the API.
__title__ = "REANA Server"

# Short description of the API. Supports GitHub Flavored Markdown.
__api_description__ = "Submit workflows to be run on REANA Cloud"

# Version of the API provides, not version of the OpenAPI specification.
__api_version__ = "0.1"

# Filepath where the OpenAPI specification file should be written to.
__output_path__ = "temp_openapi.json"


@click.command()
@click.option(
    '-d', '--dev',
    multiple=True,
    help='Optional parameters if set to TRUE, will pass generated and '
         'validated openapi specifications to reana-commons module. '
         'E.g. --dev=True.',
)
@with_appcontext
def build_openapi_spec(dev):
    """Creates an OpenAPI definition of Flask application,
    check conformity of generated definition against OpenAPI 2.0 specification
    and writes it into a file."""

    package = __title__
    desc = __api_description__
    ver = __api_version__

    # Create OpenAPI specification object
    spec = APISpec(
        title=package,
        version=ver,
        info=dict(
            description=desc
        ),
        plugins=(
            'apispec.ext.flask',
            'apispec.ext.marshmallow'
        )
    )

    # Add marshmallow schemas to the specification here
    # spec.definition('Example', schema=Example_schema)

    # Collect OpenAPI docstrings from Flask endpoints
    for key in current_app.view_functions:
        if key != 'static' and key != 'get_openapi_spec':
            spec.add_path(view=current_app.view_functions[key])

    spec_json = json.dumps(spec.to_dict(), indent=2,
                           separators=(',', ': '), sort_keys=True)

    # Output spec to JSON file
    with click.open_file(__output_path__, mode='w+',
                         encoding=None, errors='strict',
                         lazy=False, atomic=False) as output_file:

        output_file.write(spec_json)

        click.echo(
            click.style('OpenAPI specification written to {}'.format(
                output_file.name), fg='green'))

    # Check that generated spec passes validation. Done after writing to file
    # in order to give user easy way to access the possible erroneous spec.
    with open(os.path.join(os.getcwd(), __output_path__)) as output_file:

        validate_json(json.load(output_file), 'schemas/v2.0/schema.json')

        click.echo(
            click.style('OpenAPI specification validated successfully',
                        fg='green'))
        copy_openapi_specs(dev)

    return spec.to_dict()


def copy_openapi_specs(dev):
    """Copy generated and validated openapi specs to reana-commons module."""
    if dev:
        if os.environ.get('REANA_SRCDIR'):
            reana_srcdir = os.environ.get('REANA_SRCDIR')
        else:
            reana_srcdir = os.path.join('..')
        try:
            reana_commons_specs_path = os.path.join(
                reana_srcdir,
                'reana-commons',
                'reana_commons',
                'openapi_specifications')
            if os.path.exists(reana_commons_specs_path):
                if os.path.isfile(__output_path__):
                    shutil.copy(__output_path__,
                                os.path.join(reana_commons_specs_path,
                                             'reana_server.json'))
                    # copy openapi specs file as well to docs
                    shutil.copy(__output_path__,
                                os.path.join('docs', 'openapi.json'))
        except Exception as e:
            click.echo('Something went wrong, could not copy openapi '
                       'specifications to reana-commons \n{0}'.format(e))


if __name__ == '__main__':
    build_openapi_spec()
