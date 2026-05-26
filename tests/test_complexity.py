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

from reana_commons.errors import REANAKubernetesMemoryLimitExceeded

from reana_server.complexity import (
    get_workflow_min_job_memory,
    estimate_complexity,
    validate_job_memory_limits,
    workflow_compute_backends,
)


@pytest.mark.parametrize(
    "complexity,min_job_memory",
    [
        ([(8, 5), (5, 10)], 5),
        ([(1, 8589934592.0), (2, 4294967296.0)], 4294967296.0),
        ([(2, 10)], 10),
        ([], 0),
        # External backend steps have jobs=0 and should not influence the result
        ([(0, 268435456)], 0),
        ([(0, 268435456), (0, 1073741824)], 0),
        ([(0, 268435456), (1, 536870912)], 536870912),
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


@mock.patch("reana_server.complexity.REANA_KUBERNETES_JOBS_MEMORY_LIMIT", "4Gi")
@pytest.mark.parametrize(
    "backends,expected_complexity,expected_min_memory",
    [
        # All steps on an external backend: no Kubernetes jobs and no memory
        # requirement, so the Kubernetes memory check is skipped.
        (
            {"scatterA": "htcondor", "scatterB": "htcondor", "gather": "htcondor"},
            [(0, 0)],
            0,
        ),
        # Mixed: scatters run externally, gather runs on Kubernetes. Only the
        # Kubernetes step contributes to jobs and memory.
        (
            {"scatterA": "htcondor", "scatterB": "htcondor", "gather": "kubernetes"},
            [(1, 4294967296.0)],
            4294967296.0,
        ),
    ],
)
def test_estimate_complexity_snakemake_external_backend(
    snakemake_workflow_spec_loaded,
    backends,
    expected_complexity,
    expected_min_memory,
):
    """Test that external-backend Snakemake steps contribute 0 jobs."""
    wf_steps = snakemake_workflow_spec_loaded["workflow"]["specification"]["steps"]
    for step in wf_steps:
        step["compute_backend"] = backends[step["name"]]
    complexity = estimate_complexity("snakemake", snakemake_workflow_spec_loaded)
    assert complexity == expected_complexity
    assert get_workflow_min_job_memory(complexity) == expected_min_memory


@pytest.mark.parametrize(
    "complexity,should_raise",
    [
        # k8s step exceeds limit — raises
        ([(1, 2000)], True),
        # external-only step with high "memory" — no raise (not a k8s limit)
        ([(0, 2000)], False),
        # mix: external over-limit + k8s under-limit — no raise
        ([(0, 2000), (1, 500)], False),
        # mix: external under-limit + k8s over-limit — raises
        ([(0, 500), (1, 2000)], True),
        # empty — no raise
        ([], False),
    ],
)
@mock.patch(
    "reana_server.complexity.REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT_IN_BYTES", 1000
)
@mock.patch(
    "reana_server.complexity.REANA_KUBERNETES_JOBS_MAX_USER_MEMORY_LIMIT", "1Ki"
)
def test_validate_job_memory_limits(complexity, should_raise):
    """Test that validate_job_memory_limits ignores external-backend steps."""
    if should_raise:
        with pytest.raises(REANAKubernetesMemoryLimitExceeded):
            validate_job_memory_limits(complexity)
    else:
        validate_job_memory_limits(complexity)


def _serial_spec(steps):
    return {"workflow": {"specification": {"steps": steps}}}


def _yadage_spec(stages):
    return {"workflow": {"specification": {"stages": stages}}}


def _cwl_spec(steps):
    return {"workflow": {"specification": {"steps": steps}}}


def _snakemake_spec(steps):
    return {"workflow": {"specification": {"steps": steps}}}


def _yadage_stage(name, compute_backend=None, nested_stages=None):
    resources = [{"compute_backend": compute_backend}] if compute_backend else []
    stage = {
        "name": name,
        "scheduler": {
            "step": {"environment": {"resources": resources}},
        },
    }
    if nested_stages is not None:
        stage["scheduler"]["workflow"] = {"stages": nested_stages}
    return stage


@pytest.mark.parametrize(
    "workflow_type,spec,expected",
    [
        # --- serial ---
        # default backend (no compute_backend key) → kubernetes
        ("serial", _serial_spec([{"commands": ["echo hi"]}]), True),
        # explicit kubernetes
        (
            "serial",
            _serial_spec([{"commands": ["echo"], "compute_backend": "kubernetes"}]),
            True,
        ),
        # external backend only
        (
            "serial",
            _serial_spec([{"commands": ["echo"], "compute_backend": "htcondor"}]),
            False,
        ),
        # mixed: one external + one k8s
        (
            "serial",
            _serial_spec(
                [
                    {"commands": ["echo"], "compute_backend": "htcondor"},
                    {"commands": ["echo"]},
                ]
            ),
            True,
        ),
        # empty steps → conservative True
        ("serial", _serial_spec([]), True),
        # --- yadage ---
        # default backend (no resources) → kubernetes
        ("yadage", _yadage_spec([_yadage_stage("step1")]), True),
        # external backend
        (
            "yadage",
            _yadage_spec([_yadage_stage("step1", compute_backend="htcondor")]),
            False,
        ),
        # external top-level, kubernetes nested stage
        (
            "yadage",
            _yadage_spec(
                [
                    _yadage_stage(
                        "step1",
                        compute_backend="htcondor",
                        nested_stages=[_yadage_stage("nested")],
                    )
                ]
            ),
            True,
        ),
        # all external including nested
        (
            "yadage",
            _yadage_spec(
                [
                    _yadage_stage(
                        "step1",
                        compute_backend="htcondor",
                        nested_stages=[
                            _yadage_stage("nested", compute_backend="htcondor")
                        ],
                    )
                ]
            ),
            False,
        ),
        # wrapper stage with default (k8s) backend but all-external nested stages:
        # the wrapper delegates its complexity to its children, so this counts as
        # external-only
        (
            "yadage",
            _yadage_spec(
                [
                    _yadage_stage(
                        "wrapper",
                        nested_stages=[
                            _yadage_stage("nested", compute_backend="htcondor")
                        ],
                    )
                ]
            ),
            False,
        ),
        # empty stages → conservative True
        ("yadage", _yadage_spec([]), True),
        # --- snakemake ---
        # default backend → kubernetes
        ("snakemake", _snakemake_spec([{"name": "step1"}]), True),
        # external backend
        (
            "snakemake",
            _snakemake_spec([{"name": "step1", "compute_backend": "htcondor"}]),
            False,
        ),
        # mixed
        (
            "snakemake",
            _snakemake_spec(
                [
                    {"name": "step1", "compute_backend": "htcondor"},
                    {"name": "step2"},
                ]
            ),
            True,
        ),
        # empty → conservative True
        ("snakemake", _snakemake_spec([]), True),
        # --- cwl ---
        # default backend (no hints) → kubernetes
        ("cwl", _cwl_spec([{"id": "step1", "hints": []}]), True),
        # external backend via hints
        (
            "cwl",
            _cwl_spec([{"id": "step1", "hints": [{"compute_backend": "htcondor"}]}]),
            False,
        ),
        # external top-level, kubernetes step in nested sub-workflow
        (
            "cwl",
            _cwl_spec(
                [
                    {
                        "id": "step1",
                        "hints": [{"compute_backend": "htcondor"}],
                        "run": {"steps": [{"id": "nested", "hints": []}]},
                    }
                ]
            ),
            True,
        ),
        # all external, including nested
        (
            "cwl",
            _cwl_spec(
                [
                    {
                        "id": "step1",
                        "hints": [{"compute_backend": "htcondor"}],
                        "run": {
                            "steps": [
                                {
                                    "id": "nested",
                                    "hints": [{"compute_backend": "htcondor"}],
                                }
                            ]
                        },
                    }
                ]
            ),
            False,
        ),
        # empty steps → conservative True
        ("cwl", _cwl_spec([]), True),
    ],
)
def test_workflow_uses_kubernetes(workflow_type, spec, expected):
    """Test Kubernetes detection for all workflow types and backend combinations."""
    assert ("kubernetes" in workflow_compute_backends(workflow_type, spec)) == expected


@pytest.mark.parametrize(
    "workflow_type,spec,expected",
    [
        # single external backend is reported as such
        (
            "serial",
            _serial_spec([{"commands": ["echo"], "compute_backend": "htcondor"}]),
            ["htcondor"],
        ),
        # hybrid workflow reports every backend it uses, sorted
        (
            "serial",
            _serial_spec(
                [
                    {"commands": ["echo"], "compute_backend": "htcondor"},
                    {"commands": ["echo"]},
                    {"commands": ["echo"], "compute_backend": "slurm"},
                ]
            ),
            ["htcondor", "kubernetes", "slurm"],
        ),
        # default backend → kubernetes
        ("serial", _serial_spec([{"commands": ["echo"]}]), ["kubernetes"]),
        # empty → conservative kubernetes
        ("serial", _serial_spec([]), ["kubernetes"]),
        # snakemake mixed
        (
            "snakemake",
            _snakemake_spec(
                [
                    {"name": "a", "compute_backend": "htcondor"},
                    {"name": "b"},
                ]
            ),
            ["htcondor", "kubernetes"],
        ),
    ],
)
def test_workflow_compute_backends(workflow_type, spec, expected):
    """The classifier returns the sorted set of step-level compute backends."""
    assert workflow_compute_backends(workflow_type, spec) == expected
