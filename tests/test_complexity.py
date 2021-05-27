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
    calculate_workflow_priority,
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


@pytest.mark.parametrize(
    "complexity,priority,cluster_memory",
    [
        ([(1, 8589934592.0), (2, 4294967296.0), (5, 4294967296.0)], 55, 85899345920.0),
        ([(2, 8)], 96, 400),
        ([(1, 6), (1, 6)], 97, 400),
        ([(5, 2), (5, 2)], 95, 400),
        ([(1, 396)], 1, 400),
        ([(1, 401)], 0, 400),
        ([], 0, 100),
        ([(3, 5), (1, 5)], 80, 100),
        ([(3, 20), (2, 10)], 20, 100),
        ([(2, 20), (5, 1)], 55, 100),
        ([(1, 1), (1, 1)], 98, 100),
        ([(1, 10), (1, 20), (1, 5), (1, 5), (1, 10), (1, 10)], 40, 100),
    ],
)
def test_calculate_workflow_priority(complexity, priority, cluster_memory):
    """Test calculate_workflow_priority."""
    with patch(
        "reana_server.status.NodesStatus.get_total_memory", return_value=cluster_memory,
    ):
        assert calculate_workflow_priority(complexity) == priority


def test_estimate_complexity(yadage_workflow_spec_loaded):
    """Test estimate_complexity."""
    assert estimate_complexity("yadage", yadage_workflow_spec_loaded) == [
        (1, 4294967296.0)
    ]
