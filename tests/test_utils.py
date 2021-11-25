# This file is part of REANA.
# Copyright (C) 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server tests for utils module."""

import pytest
from reana_server.utils import is_valid_email


@pytest.mark.parametrize(
    "email,is_valid",
    [
        ("john@example.org", True),
        ("john.doe@example.org", True),
        ("john-doe@example.org", True),
        ("john.doe@edu.uni.org", True),
        ("jean-yves.le.meur@cern.ch", True),
        ("john.doe@exampleorg", False),
        ("john.doeexample.org", False),
        ("john@example.org.", False),
        ("john@example..org", False),
        ("john@@example.org", False),
    ],
)
def test_is_email_valid(email: str, is_valid: bool):
    assert is_valid_email(email) == is_valid
