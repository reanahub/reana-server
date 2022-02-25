# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server validation utilities."""

from typing import Dict

from reana_commons.validation.parameters import build_parameters_validator
from reana_commons.validation.compute_backends import build_compute_backends_validator
from reana_commons.validation.utils import validate_workspace
from reana_commons.config import WORKSPACE_PATHS

from reana_server.config import SUPPORTED_COMPUTE_BACKENDS


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
