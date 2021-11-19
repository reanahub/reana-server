# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Test workflow complexity estimation."""

import mock
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


@mock.patch("reana_server.complexity.REANA_KUBERNETES_JOBS_MEMORY_LIMIT", "4Gi")
@mock.patch("reana_server.utils.REANA_WORKFLOW_SCHEDULING_POLICY", "balanced")
def test_estimate_complexity(yadage_workflow_spec_loaded):
    """Test estimate_complexity."""
    assert estimate_complexity("yadage", yadage_workflow_spec_loaded) == [
        (1, 4294967296.0)
    ]


@pytest.mark.parametrize(
    "job_deps, scatterA_mem, scatterB_mem, gather_mem, complexity",
    [
        (None, "6Gi", "4Gi", None, (2, 5368709120.0)),
        (None, "2Gi", "2Gi", "1Gi", (2, 2147483648.0)),
        (
            {
                "all": ["gather"],
                "gather": ["scatterA", "scatterB"],
                "scatterA": [],
                "scatterB": ["scatterA"],  # no paralellization
            },
            "2Gi",
            "2Gi",
            "1Gi",
            (1, 2147483648.0),
        ),
        # 4.1Gi > 2Gi + 2Gi - gather job consumes more than two parallel scatter jobs
        (None, "2Gi", "2Gi", "4.1Gi", (1, 4402341478.4)),
    ],
)
@mock.patch("reana_server.complexity.REANA_KUBERNETES_JOBS_MEMORY_LIMIT", "4Gi")
def test_estimate_complexity_snakemake(
    snakemake_workflow_spec_loaded,
    job_deps,
    scatterA_mem,
    scatterB_mem,
    gather_mem,
    complexity,
):
    """Test ``estimate_complexity`` in Snakemake workflows."""
    if job_deps:
        snakemake_workflow_spec_loaded["workflow"]["specification"][
            "job_dependencies"
        ] = job_deps
    wf_steps = snakemake_workflow_spec_loaded["workflow"]["specification"]["steps"]
    # scatter A
    wf_steps[0]["kubernetes_memory_limit"] = scatterA_mem
    # scatter B
    wf_steps[1]["kubernetes_memory_limit"] = scatterB_mem
    # gather
    wf_steps[2]["kubernetes_memory_limit"] = gather_mem
    assert estimate_complexity("snakemake", snakemake_workflow_spec_loaded) == [
        complexity
    ]
