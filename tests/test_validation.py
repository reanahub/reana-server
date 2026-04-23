# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server tests for validation module."""

import pytest
from unittest.mock import patch
from contextlib import nullcontext as does_not_raise

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


@pytest.mark.parametrize(
    "config, images, error",
    [
        # Validation disabled, anything goes
        (
            {"enabled": False, "allowlist": []},
            ["docker.io/bitcoin-miner:1.2.3"],
            does_not_raise(),
        ),
        # Allowed image
        (
            {
                "enabled": True,
                "allowlist": ["docker.io/reanahub/reana-env-root6:6.18.04"],
            },
            ["docker.io/reanahub/reana-env-root6:6.18.04"],
            does_not_raise(),
        ),
        # Disallowed image
        (
            {
                "enabled": True,
                "allowlist": ["docker.io/reanahub/reana-env-root6:6.18.04"],
            },
            ["docker.io/bitcoin-miner:1.2.3"],
            pytest.raises(REANAValidationError, match="not allowed"),
        ),
        # Mixed images
        (
            {
                "enabled": True,
                "allowlist": ["docker.io/reanahub/reana-env-root6:6.18.04"],
            },
            [
                "docker.io/reanahub/reana-env-root6:6.18.04",
                "docker.io/bitcoin-miner:1.2.3",
            ],
            pytest.raises(REANAValidationError, match="not allowed"),
        ),
    ],
)
def test_validate_images(config, images, error):
    with patch("reana_server.validation.REANA_VETTED_CONTAINER_IMAGES", config):
        with error:
            validate_images(
                {
                    "workflow": {
                        "specification": {
                            "steps": [{"environment": image} for image in images]
                        }
                    }
                }
            )


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
