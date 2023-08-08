# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server validation utilities."""

import itertools
import pathlib
from typing import Dict, List

from reana_commons.config import WORKSPACE_PATHS
from reana_commons.errors import REANAValidationError
from reana_commons.validation.compute_backends import build_compute_backends_validator
from reana_commons.validation.operational_options import validate_operational_options
from reana_commons.validation.parameters import build_parameters_validator
from reana_commons.validation.utils import validate_reana_yaml, validate_workspace

from reana_server.config import SUPPORTED_COMPUTE_BACKENDS, WORKSPACE_RETENTION_PERIOD
from reana_server import utils


def validate_parameters(reana_yaml: Dict) -> None:
    """Validate the presence of input parameters in workflow step commands and viceversa.

    :param reana_yaml: REANA YAML specification.

    :raises REANAValidationError: Given there are parameter validation errors in REANA spec file.
    """
    validator = build_parameters_validator(reana_yaml)
    validator.validate_parameters()


def validate_workspace_path(reana_yaml: Dict) -> None:
    """Validate workspace in REANA specification file.

    :param reana_yaml: REANA YAML specification.

    :raises REANAValidationError: Given workspace in REANA spec file does not validate against
        allowed workspaces.
    """
    root_path = reana_yaml.get("workspace", {}).get("root_path")
    if root_path:
        available_paths = list(WORKSPACE_PATHS.values())
        validate_workspace(root_path, available_paths)


def validate_compute_backends(reana_yaml: Dict) -> None:
    """Validate compute backends in REANA specification file according to workflow type.

    :param reana_yaml: dictionary which represents REANA specification file.

    :raises REANAValidationError: Given compute backend specified in REANA spec file does not validate against
        supported compute backends.
    """
    validator = build_compute_backends_validator(reana_yaml, SUPPORTED_COMPUTE_BACKENDS)
    validator.validate()


def validate_input_parameters(
    input_parameters: Dict, original_parameters: Dict
) -> Dict:
    """Validate input parameters.

    :param input_parameters: dictionary which represents additional workflow input parameters.
    :param original_parameters: dictionary which represents original workflow input parameters.

    :raises REANAValidationError: Given there are additional input parameters which are not present in the REANA spec parameter list.
    """
    for parameter in input_parameters.keys():
        if parameter not in original_parameters:
            raise REANAValidationError(
                f'Input parameter "{parameter}" is not present in reana.yaml'
            )
    return input_parameters


def validate_inputs(reana_yaml: Dict) -> None:
    """Check whether the paths of the input files/directories are valid or not.

    :param reana_yaml: REANA specification.
    """
    inputs = reana_yaml.get("inputs", {})
    files = inputs.get("files", [])
    directories = inputs.get("directories", [])
    paths = [pathlib.Path(path) for path in files + directories]

    unique_paths = set()
    for path in paths:
        if path.is_absolute():
            raise REANAValidationError(f"Input path cannot be absolute: {path}")
        if not path.parts:
            raise REANAValidationError("Input path cannot be empty")
        if ".." in path.parts:
            raise REANAValidationError(f"Input path cannot contain '..': {path}")
        if path in unique_paths:
            raise REANAValidationError(f"Input path declared multiple times: {path}")
        unique_paths.add(path)

    for x, y in itertools.permutations(paths, r=2):
        if utils.is_relative_to(x, y):
            raise REANAValidationError(
                f"Duplicate input paths '{y}' and '{x}' found. Please deduplicate inputs first."
            )


def validate_workflow(reana_yaml: Dict, input_parameters: Dict) -> Dict:
    """Validate REANA workflow specification by calling all the validation utilities.

    :param reana_yaml: dictionary which represents REANA specification file.
    :param input_parameters: dictionary which represents additional workflow input parameters.

    :raises REANAValidationError: Given there are validation errors in REANA spec file.
    """
    workflow_type = reana_yaml["workflow"]["type"]
    operational_options = reana_yaml.get("inputs", {}).get("options", {})
    original_parameters = reana_yaml.get("inputs", {}).get("parameters", {})

    reana_yaml_warnings = validate_reana_yaml(reana_yaml)
    validate_operational_options(workflow_type, operational_options)
    validate_input_parameters(input_parameters, original_parameters)
    validate_compute_backends(reana_yaml)
    validate_workspace_path(reana_yaml)
    validate_inputs(reana_yaml)
    return reana_yaml_warnings


def validate_retention_rule(rule: str, days: int) -> None:
    """Validate retention rule.

    :param rule: retention rule
    :type rule: str

    :param days: after how many days rules need to be applied
    :type days: int

    :raises reana_commons.errors.REANAValidationError: if rule is not valid
    """
    rule_path = pathlib.Path(rule)
    if rule_path.is_absolute():
        raise REANAValidationError(f"Retention rule {rule} cannot be an absolute path")
    if not rule_path.parts:
        raise REANAValidationError(f"Retention rule {rule} cannot be empty")
    if ".." in rule_path.parts:
        raise REANAValidationError(f"Retention rule {rule} cannot contain '..'")

    default_retention_rules_are_not_disabled = WORKSPACE_RETENTION_PERIOD is not None
    if default_retention_rules_are_not_disabled and days >= WORKSPACE_RETENTION_PERIOD:
        raise REANAValidationError(
            "Maximum workflow retention period was reached. "
            f"Please use less than {WORKSPACE_RETENTION_PERIOD} days."
        )
