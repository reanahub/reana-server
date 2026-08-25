# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Contract check: every status a route returns is declared in its OpenAPI docstring.

This is a purely source-based guard (no route/spec mapping needed): for each
view function it collects the integer status codes returned literally in the
body and asserts each is declared in that function's own apispec ``responses``
block. It exists to stop drift like a route returning ``409`` while its spec
still advertises only ``201/403/500`` -- a Bravado client would raise
``MatchingResponseNotFound`` instead of surfacing the server's message.

Dynamic statuses (e.g. ``return resp.content, resp.status_code``) cannot be
resolved statically and are ignored; the guard covers literal returns, which is
where the observed drift occurred.

The literal-return scan is blind to statuses a *decorator* injects around the
view function -- ``signin_required`` returns 401/403/500/503 from its
wrapper in ``decorators.py`` (500 for ``IssuerMisconfiguredError``), never
touching the route function's own AST, and ``check_quota`` similarly
injects 403/500. A route can therefore pass the literal-return check while
still failing to declare a status its own decorator is guaranteed to
produce. ``_decorator_injected_codes`` closes that gap by checking each
function's decorator list.
"""

import ast
import pathlib

import pytest
import yaml

REST_DIR = pathlib.Path(__file__).resolve().parents[1] / "reana_server" / "rest"


def _returned_status_literals(func_node):
    """Collect integer status literals from ``return ..., <int>`` and ``abort(<int>)``."""
    codes = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            if len(node.value.elts) >= 2:
                last = node.value.elts[-1]
                if isinstance(last, ast.Constant) and isinstance(last.value, int):
                    codes.add(last.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "abort"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, int)
        ):
            codes.add(node.args[0].value)
    return codes


def _declared_status_codes(docstring):
    """Return the set of response codes declared in an apispec docstring, or None."""
    if not docstring or "\n---\n" not in docstring:
        return None
    _, _, spec_text = docstring.partition("\n---\n")
    try:
        spec = yaml.safe_load(spec_text)
    except yaml.YAMLError:
        return set()
    if not isinstance(spec, dict):
        return set()
    codes = set()
    for definition in spec.values():
        if isinstance(definition, dict) and isinstance(
            definition.get("responses"), dict
        ):
            codes.update(
                code for code in definition["responses"] if isinstance(code, int)
            )
    return codes


# Status codes each decorator's wrapper can produce around the view function,
# regardless of what the view function itself returns. Keep in sync with
# reana_server/decorators.py: ``signin_required``'s wrapper returns 401 (no
# credentials / invalid token), 403 (missing role / CSRF / provisioning
# error), and 503 (issuer or session-store unavailable); ``check_quota``'s
# wrapper returns 403 (quota exceeded) and 500 (unexpected error).
_DECORATOR_CODES = {
    "signin_required": {401, 403, 500, 503},
    "check_quota": {403, 500},
}


def _decorator_names(func_node):
    """Return the plain names of a function's decorators (calls or bare)."""
    names = []
    for decorator in func_node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
    return names


def _decorator_injected_codes(func_node):
    """Return the union of status codes every known decorator on this function injects."""
    codes = set()
    for name in _decorator_names(func_node):
        codes |= _DECORATOR_CODES.get(name, set())
    return codes


def _view_functions():
    """Yield ``(module, function, docstring)`` for every apispec-documented view."""
    for path in sorted(REST_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                docstring = ast.get_docstring(node)
                if _declared_status_codes(docstring) is not None:
                    yield path.name, node, docstring


def test_returned_statuses_are_declared_in_the_openapi_docstring():
    """Every literal status a route returns must be declared in its spec block."""
    violations = []
    for module, func, docstring in _view_functions():
        declared = _declared_status_codes(docstring)
        returned = _returned_status_literals(func)
        undeclared = returned - declared
        if undeclared:
            violations.append(
                f"{module}:{func.name} returns {sorted(undeclared)} "
                f"not declared in its responses (declared: {sorted(declared)})"
            )
    assert not violations, "Undeclared response statuses:\n" + "\n".join(violations)


def test_decorator_injected_statuses_are_declared_in_the_openapi_docstring():
    """Every status a route's own decorators can produce must be declared too.

    The literal-return scan above cannot see this class of drift: it only
    looks inside the view function's own body, never at what
    ``signin_required``/``check_quota`` return from their wrapper. This is
    what would have caught PR789-30's sibling gap (28 of 36 signin_required
    routes not declaring 401) on its own, without a human having to notice.
    """
    violations = []
    for module, func, docstring in _view_functions():
        expected = _decorator_injected_codes(func)
        if not expected:
            continue
        declared = _declared_status_codes(docstring)
        undeclared = expected - declared
        if undeclared:
            violations.append(
                f"{module}:{func.name} decorators {_decorator_names(func)} can "
                f"return {sorted(undeclared)}, not declared in its responses "
                f"(declared: {sorted(declared)})"
            )
    assert not violations, "Undeclared decorator-injected statuses:\n" + "\n".join(
        violations
    )


def test_guard_catches_a_decorator_protected_route_missing_401():
    """The guard must fail on a route that only declares what its body returns.

    This is exactly the PR789-30 sibling gap: the view function itself only
    ever ``return``s 200, so the literal-return check
    (``test_returned_statuses_are_declared_in_the_openapi_docstring``) is
    satisfied even though ``signin_required`` can still produce
    401/403/500/503 around it. Proves the decorator-aware check is the one
    doing the work.
    """
    broken_source = '''
@signin_required()
def broken_route(user):
    """Broken route.

    ---
    get:
      responses:
        200:
          description: OK.
    """
    return "ok", 200
'''
    func_node = ast.parse(broken_source).body[0]
    docstring = ast.get_docstring(func_node)

    assert _returned_status_literals(func_node) <= _declared_status_codes(docstring)

    expected = _decorator_injected_codes(func_node)
    declared = _declared_status_codes(docstring)
    assert expected - declared == {401, 403, 500, 503}
