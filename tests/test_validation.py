# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server tests for validation module."""

import pytest
from unittest.mock import patch
from contextlib import nullcontext as does_not_raise

from reana_commons.config import REANA_DEFAULT_SNAKEMAKE_ENV_IMAGE
from reana_commons.errors import REANAValidationError

from reana_server.validation import (
    validate_inputs,
    validate_images,
    validate_retention_rule,
)


@pytest.mark.parametrize(
    "paths, error",
    [
        (["/absolute/path"], "absolute"),
        (["invalid/../path"], r"\.\."),
        ([""], "empty"),
        (["dir", "dir/xyz"], "Duplicate"),
        (["dir", "dir/"], "multiple"),
    ],
)
def test_validate_inputs(paths, error):
    with pytest.raises(REANAValidationError, match=error):
        validate_inputs({"inputs": {"directories": paths}})


ALLOWLIST = {
    "enabled": True,
    "allowlist": ["docker.io/reanahub/reana-env-root6:6.18.04"],
}
DISALLOWED_IMAGE = "docker.io/bitcoin-miner:1.2.3"
ALLOWED_IMAGE = "docker.io/reanahub/reana-env-root6:6.18.04"


def serial_workflow(*images):
    return {
        "type": "serial",
        "specification": {"steps": [{"environment": img} for img in images]},
    }


def snakemake_workflow(*images):
    return {
        "type": "snakemake",
        "specification": {"steps": [{"environment": img} for img in images]},
    }


@pytest.mark.parametrize(
    "config, workflow, error",
    [
        pytest.param(
            {"enabled": False, "allowlist": []},
            serial_workflow(DISALLOWED_IMAGE),
            does_not_raise(),
            id="disabled-anything-goes",
        ),
        # Serial: explicit environment field
        pytest.param(
            ALLOWLIST,
            serial_workflow(ALLOWED_IMAGE),
            does_not_raise(),
            id="serial-allowed",
        ),
        pytest.param(
            ALLOWLIST,
            serial_workflow(DISALLOWED_IMAGE),
            pytest.raises(REANAValidationError, match="not allowed"),
            id="serial-disallowed",
        ),
        pytest.param(
            ALLOWLIST,
            serial_workflow(ALLOWED_IMAGE, DISALLOWED_IMAGE),
            pytest.raises(REANAValidationError, match="not allowed"),
            id="serial-mixed",
        ),
        # Snakemake: rules with explicit container directive
        pytest.param(
            ALLOWLIST,
            snakemake_workflow(ALLOWED_IMAGE),
            does_not_raise(),
            id="snakemake-explicit-container-allowed",
        ),
        pytest.param(
            ALLOWLIST,
            snakemake_workflow(DISALLOWED_IMAGE),
            pytest.raises(REANAValidationError, match="not allowed"),
            id="snakemake-explicit-container-disallowed",
        ),
        # Snakemake: the runtime default must be explicitly allowlisted.
        pytest.param(
            {"enabled": True, "allowlist": []},
            snakemake_workflow(""),
            pytest.raises(REANAValidationError, match="not allowed"),
            id="snakemake-no-container-empty-allowlist-rejected",
        ),
        pytest.param(
            {
                "enabled": True,
                "allowlist": [REANA_DEFAULT_SNAKEMAKE_ENV_IMAGE],
            },
            snakemake_workflow(""),
            does_not_raise(),
            id="snakemake-no-container-default-allowlisted",
        ),
        pytest.param(
            {
                "enabled": True,
                "allowlist": [
                    ALLOWED_IMAGE,
                    REANA_DEFAULT_SNAKEMAKE_ENV_IMAGE,
                ],
            },
            snakemake_workflow(ALLOWED_IMAGE, ""),
            does_not_raise(),
            id="snakemake-mixed-with-and-without-container",
        ),
        pytest.param(
            ALLOWLIST,
            snakemake_workflow(DISALLOWED_IMAGE, ""),
            pytest.raises(REANAValidationError, match="not allowed"),
            id="snakemake-mixed-disallowed-explicit-plus-no-container",
        ),
        # CWL: images come from requirements[].dockerPull, not steps
        pytest.param(
            ALLOWLIST,
            {
                "type": "cwl",
                "specification": {
                    "$graph": [
                        {
                            "class": "Workflow",
                            "requirements": [
                                {
                                    "class": "DockerRequirement",
                                    "dockerPull": ALLOWED_IMAGE,
                                }
                            ],
                        }
                    ]
                },
            },
            does_not_raise(),
            id="cwl-allowed",
        ),
        pytest.param(
            ALLOWLIST,
            {
                "type": "cwl",
                "specification": {
                    "$graph": [
                        {
                            "class": "Workflow",
                            "requirements": [
                                {
                                    "class": "DockerRequirement",
                                    "dockerPull": DISALLOWED_IMAGE,
                                }
                            ],
                        }
                    ]
                },
            },
            pytest.raises(REANAValidationError, match="not allowed"),
            id="cwl-disallowed",
        ),
        pytest.param(
            {"enabled": True, "allowlist": []},
            {
                "type": "cwl",
                "specification": {
                    "$graph": [{"class": "Workflow", "requirements": []}]
                },
            },
            does_not_raise(),
            id="cwl-no-docker-requirement",
        ),
        # Yadage: images come from nested stages, not a flat steps list
        pytest.param(
            ALLOWLIST,
            {
                "type": "yadage",
                "specification": {
                    "stages": [
                        {
                            "name": "stage1",
                            "scheduler": {
                                "step": {
                                    "environment": {
                                        "environment_type": "docker-encapsulated",
                                        "image": "docker.io/reanahub/reana-env-root6",
                                        "imagetag": "6.18.04",
                                    }
                                }
                            },
                        }
                    ]
                },
            },
            does_not_raise(),
            id="yadage-allowed",
        ),
        pytest.param(
            ALLOWLIST,
            {
                "type": "yadage",
                "specification": {
                    "stages": [
                        {
                            "name": "stage1",
                            "scheduler": {
                                "step": {
                                    "environment": {
                                        "environment_type": "docker-encapsulated",
                                        "image": "docker.io/bitcoin-miner",
                                        "imagetag": "1.2.3",
                                    }
                                }
                            },
                        }
                    ]
                },
            },
            pytest.raises(REANAValidationError, match="not allowed"),
            id="yadage-disallowed",
        ),
        pytest.param(
            ALLOWLIST,
            {
                "type": "yadage",
                "specification": {
                    "stages": [
                        {
                            "name": "outer",
                            "scheduler": {
                                "workflow": {
                                    "stages": [
                                        {
                                            "name": "inner",
                                            "scheduler": {
                                                "step": {
                                                    "environment": {
                                                        "environment_type": "docker-encapsulated",
                                                        "image": "docker.io/bitcoin-miner",
                                                        "imagetag": "1.2.3",
                                                    }
                                                }
                                            },
                                        }
                                    ]
                                }
                            },
                        }
                    ]
                },
            },
            pytest.raises(REANAValidationError, match="not allowed"),
            id="yadage-nested-stage-disallowed",
        ),
        pytest.param(
            {"enabled": True, "allowlist": []},
            {"type": "yadage", "specification": {"stages": []}},
            does_not_raise(),
            id="yadage-no-stages",
        ),
    ],
)
def test_validate_images(config, workflow, error):
    with patch("reana_server.validation.REANA_VETTED_CONTAINER_IMAGES", config):
        with error:
            validate_images({"workflow": workflow})


@pytest.mark.parametrize(
    "rule, days, error",
    [
        ("**/*", 10, does_not_raise()),
        (
            "data/results/*",
            30000,
            pytest.raises(REANAValidationError, match="Maximum workflow retention"),
        ),
        ("/etc/*", 10, pytest.raises(REANAValidationError, match="absolute")),
        ("./", 10, pytest.raises(REANAValidationError, match="empty")),
        ("../**/*", 10, pytest.raises(REANAValidationError, match="'..'")),
    ],
)
@patch("reana_server.validation.WORKSPACE_RETENTION_PERIOD", 365)
def test_validate_retention_rule(rule, days, error):
    with error:
        validate_retention_rule(rule, days)
