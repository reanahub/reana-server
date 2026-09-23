# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test the launch endpoint OpenAPI specification."""

import json
from pathlib import Path


def test_launch_validation_warnings_schema():
    """Test structured launch validation warnings in OpenAPI."""
    specification_path = Path(__file__).parents[1] / "docs" / "openapi.json"
    with specification_path.open() as specification_file:
        specification = json.load(specification_file)

    schema = specification["paths"]["/api/launch"]["post"]["responses"]["200"][
        "schema"
    ]["properties"]["validation_warnings"]

    assert schema["type"] == "array"
    assert schema["items"]["type"] == "object"
    assert schema["items"]["properties"] == {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "path": {"type": "string"},
    }
