# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

import pytest

from reana_server.config import _is_valid_rate_limit

correct_rate_limit_values = [
    ("20 per second", True),
    ("100 per minute", True),
    ("6000 per hour", True),
    ("20/second", True),
    ("20per second", False),
    ("100 perminute", False),
    ("20second", False),
    ("20", False),
    ("second", False),
    ("", False),
]


@pytest.mark.parametrize("value,is_valid", correct_rate_limit_values)
def test_is_valid_rate_limit(value: str, is_valid: bool):
    assert _is_valid_rate_limit(value) == is_valid
