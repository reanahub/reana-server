#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022, 2023 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""OpenAPI specification checker."""

import ast
import inspect
import json
import pkgutil
import sys
from typing import Set

import click
from apispec.utils import load_yaml_from_docstring
from flask import current_app
from flask.cli import with_appcontext


def load_rwc_spec():
    """Load and parse the OpenAPI specification of RWC."""
    spec = pkgutil.get_data(
        "reana_commons", "openapi_specifications/reana_workflow_controller.json"
    )
    if not spec:
        raise RuntimeError("Cannot load RWC specification")
    return json.loads(spec)


class ViewVisitor(ast.NodeVisitor):
    """Class that analyses a view function by visiting the nodes of the AST.

    This class visits all the nodes of the AST of a view function in order to collect
    details about the view itself. These details will then be used by the ``Checker`` to
    detect possible mistakes in the OpenAPI specification.
    """

    def __init__(self):
        """Initialize a new ViewVisitor."""
        super().__init__()
        self.called_rwc_operations: Set[str] = set()
        """Set of RWC methods called by the view."""
        self.status_codes: Set[int] = set()
        """Set of status codes returned by the view."""
        self.returns_rwc_status_code: bool = False
        """True if the view propagates status codes returned by requests to RWC, False otherwise."""
        self.signin_required: bool = False
        """True if the ``signin_required`` decorator is applied to the view, False otherwise."""
        self.unknown_return: bool = False
        """True if there are return statements whose status code cannot be determined, False otherwise."""

    def check_rwc_method(self, node: ast.Call) -> None:
        """Check if the given function call is a request to RWC.

        Calls to RWC are usually in the form of
        ``current_rwc_api_client.api.<operation_id>(...)``.
        """
        # AST of `current_rwc_api_client.api.<operation_id>(...)`:
        #
        # ast.Call(
        #     func=ast.Attribute(
        #         attr="<operation_id>",
        #         value=ast.Attribute(
        #             attr="api", value=ast.Name(id="current_rwc_api_client")
        #         ),
        #     )
        # )
        if not isinstance(node.func, ast.Attribute):
            return
        operation_id = node.func.attr
        api_node = node.func.value
        if (
            not isinstance(api_node, ast.Attribute)
            or api_node.attr != "api"
            or not isinstance(api_node.value, ast.Name)
            or api_node.value.id != "current_rwc_api_client"
        ):
            return
        # Store name of the RWC request
        self.called_rwc_operations.add(operation_id)

    def check_status_code(self, node: ast.Return) -> None:
        """Check which status code is returned.

        Two cases are considered:
        - ``return response, <status_code>``, where ``<status_code>`` is an integer
        - ``return response, [...].status_code``, used to propagate the status code returned
          by a call to RWC
        """
        # Check that the returned value is a tuple with two elements
        ret_value = node.value
        if not isinstance(ret_value, ast.Tuple) or len(ret_value.elts) != 2:
            self.unknown_return = True
            return

        # The status code is the second element of the returned tuple
        status_code = ret_value.elts[1]
        if isinstance(status_code, ast.Attribute) and status_code.attr == "status_code":
            # probably returning error coming from RWC
            self.returns_rwc_status_code = True
        elif isinstance(status_code, ast.Constant) and isinstance(
            status_code.value, int
        ):
            # e.g. return ..., 404
            self.status_codes.add(status_code.value)
        else:
            self.unknown_return = True

    def check_signin_required(self, node: ast.FunctionDef) -> None:
        """Check whether `signin_required` decorator is applied to the view function."""
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "signin_required"
            ):
                self.signin_required = True

    def visit_Return(self, node: ast.Return) -> None:
        """Visit all the ``Return`` nodes of the AST."""
        self.check_status_code(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Visit all the ``Call`` nodes of the AST."""
        self.check_rwc_method(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Visit all the ``FunctionDef`` nodes of the AST."""
        self.check_signin_required(node)
        self.generic_visit(node)


class Checker:
    """Check view functions to find common mistakes in their OpenAPI specification."""

    rwc_spec = load_rwc_spec()

    @classmethod
    def get_rwc_status_codes(cls, operation_id):
        """Get the possible status codes returned by a RWC operation."""
        for path in cls.rwc_spec["paths"]:
            for operation in cls.rwc_spec["paths"][path].values():
                if operation["operationId"] == operation_id:
                    return set(map(int, operation["responses"].keys()))
        raise ValueError("RWC operation not found")

    def __init__(self):
        """Initialize a new Checker."""
        self.errors: bool = False

    def info(self, msg: str) -> None:
        """Print an info message."""
        print(msg)

    def warning(self, msg: str) -> None:
        """Print a warning message."""
        click.secho(f"  --> {msg}", fg="yellow")

    def error(self, msg: str) -> None:
        """Print an error message."""
        self.errors = True
        click.secho(f"  --> {msg}", fg="red")

    def check_view(self, name: str, view) -> None:
        """Check for errors in the OpenAPI specification of the given view."""
        self.info(f"Checking {name}")

        spec = load_yaml_from_docstring(view.__doc__)
        if not spec:
            self.warning("no specification found")
            return

        # Get the set of returned status codes in the OpenAPI specification
        spec_codes = {code for op in spec.values() for code in op["responses"]}

        # Parse the view code
        tree = ast.parse(inspect.getsource(view))
        visitor = ViewVisitor()
        visitor.visit(tree)

        # Show warning if view makes request to RWC, but it does not propagate the returned status code
        if visitor.called_rwc_operations and not visitor.returns_rwc_status_code:
            self.warning("detected request to RWC, but status code is not propagated")

        if visitor.unknown_return:
            self.warning("some returned status codes cannot be determined")

        if visitor.returns_rwc_status_code:
            # Check that all the status codes returned by RWC are present in the specification
            for operation_id in visitor.called_rwc_operations:
                rwc_codes = self.get_rwc_status_codes(operation_id)
                missing_codes = rwc_codes - spec_codes
                for code in missing_codes:
                    self.error(f"missing {code} returned by `{operation_id}` of RWC")

        # Check that all the returned status codes are present in the specification
        for code in visitor.status_codes:
            if code not in spec_codes:
                self.error(f"missing {code} returned by view")

        if visitor.signin_required:
            # `signin_required` returns 401 and 403 if credentials are not valid
            for code in (401, 403):
                if code not in spec_codes:
                    self.error(f"missing {code} returned by `signin_required`")


@click.command()
@with_appcontext
def check_openapi():
    """Check the OpenAPI specification for common mistakes."""
    checker = Checker()
    for name, view in current_app.view_functions.items():
        checker.check_view(name, view)
    if checker.errors:
        sys.exit(1)


if __name__ == "__main__":
    check_openapi()
