# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test workflow complexity estimation."""

import pytest
from mock import patch

from reana_server.complexity import (
    get_workflow_min_job_memory,
    estimate_complexity,
)


@pytest.mark.parametrize(
    "complexity,min_job_memory",
    [
        ([(8, 5), (5, 10)], 5),
        ([(1, 8589934592.0), (2, 4294967296.0)], 4294967296.0),
        ([(2, 10)], 10),
        ([], 0),
    ],
)
def test_get_workflow_min_job_memory(complexity, min_job_memory):
    """Test get_workflow_min_job_memory."""
    assert get_workflow_min_job_memory(complexity) == min_job_memory


def test_estimate_complexity(yadage_workflow_spec_loaded):
    """Test estimate_complexity."""
    assert estimate_complexity("yadage", yadage_workflow_spec_loaded) == [
        (1, 4294967296.0)
    ]
