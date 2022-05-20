# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server tests for validation module."""

import pytest

from reana_commons.errors import REANAValidationError

from reana_server.validation import validate_inputs


@pytest.mark.parametrize(
    "paths, error",
    [
        (["/absolute/path"], "absolute"),
        (["invalid/../path"], r"\.\."),
        ([""], "empty"),
        (["dir", "dir/xyz"], "prefix"),
        (["dir", "dir/"], "multiple"),
    ],
)
def test_validate_inputs(paths, error):
    with pytest.raises(REANAValidationError, match=error):
        validate_inputs({"inputs": {"directories": paths}})
