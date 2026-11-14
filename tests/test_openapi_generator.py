# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server OpenAPI specification generator tests."""

import importlib.util
import pathlib

import click
import pytest

_GENERATOR_PATH = (
    pathlib.Path(__file__).parent.parent / "scripts" / "generate_openapi_spec.py"
)


@pytest.fixture(scope="module")
def generator():
    """Load the generator script, which is not part of the package."""
    spec = importlib.util.spec_from_file_location("generator", _GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _view(docstring):
    """Return a view function with the given docstring."""

    def view():
        pass

    view.__doc__ = docstring
    return view


def test_repeated_response_code_fails(generator):
    """A response code declared twice stops the generation."""
    view = _view("""Example view.

        ---
        get:
          responses:
            503:
              description: The identity provider is unavailable.
            503:
              description: The workspace lock is unavailable.
        """)
    with pytest.raises(click.ClickException, match="repeated key 503"):
        generator.check_unique_docstring_keys("example", view)


def test_unique_keys_pass(generator):
    """Repeated keys in different mappings and plain docstrings are accepted."""
    view = _view("""Example view.

        ---
        get:
          responses:
            200:
              description: OK.
        post:
          responses:
            200:
              description: OK.
        """)
    generator.check_unique_docstring_keys("example", view)
    generator.check_unique_docstring_keys("plain", _view("Plain docstring."))
    generator.check_unique_docstring_keys("none", _view(None))
