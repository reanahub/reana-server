# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server cluster status tests."""

from unittest.mock import patch

import pytest

from reana_server.status import ClusterHealth


@pytest.fixture
def workflow_health():
    """Return a factory rendering the workflow health tiles for given caps."""

    def _render(caps, active_counts, dask_enabled=True):
        with patch("reana_server.status.WorkflowsStatus") as workflows_status, patch(
            "reana_server.status.Workflow.count_active_per_backend",
            return_value=active_counts,
        ), patch(
            "reana_server.status.get_concurrent_workflows_cap",
            side_effect=lambda resource: caps[resource],
        ), patch(
            "reana_server.status.SUPPORTED_COMPUTE_BACKENDS",
            [key for key in caps if key != "dask"],
        ), patch(
            "reana_server.status.DASK_ENABLED", dask_enabled
        ):
            workflows_status.return_value.get_workflows_by_status.return_value = 0
            # ``ClusterHealth.__init__`` gathers node, job and session health as
            # well, which needs a live Kubernetes cluster, so build a bare
            # instance and exercise the workflow tiles on their own.
            cluster_health = ClusterHealth.__new__(ClusterHealth)
            return cluster_health.get_workflow_health()

    return _render


def test_workflow_health_reports_headroom(workflow_health):
    """Backends below their cap report the free headroom as availability."""
    health = workflow_health(
        {"kubernetes": 30, "htcondorcern": 200, "dask": 5},
        {"kubernetes": 15, "htcondorcern": 10},
    )

    assert health["backends"]["kubernetes"]["percentage"] == 50
    assert health["backends"]["kubernetes"]["available"] == 15
    assert health["backends"]["htcondorcern"]["percentage"] == 95
    # The most-constrained resource is surfaced at the top level.
    assert health["bottleneck"] == "kubernetes"
    assert health["percentage"] == 50
    assert health["health"] == "warning"


def test_workflow_health_reports_closed_backend_as_unavailable(workflow_health):
    """A cap of zero closes the backend, so no headroom is reported.

    ``get_percentage`` treats a zero total as an unknown total and would
    otherwise report 100% available, contradicting the scheduler, which admits
    nothing at all for such a backend, as well as the per-backend tooltip in
    reana-ui, which computes 0%.
    """
    health = workflow_health(
        {"kubernetes": 30, "htcondorcern": 0, "dask": 5},
        {"kubernetes": 1},
    )

    closed = health["backends"]["htcondorcern"]
    assert closed["total"] == 0
    assert closed["used"] == 0
    assert closed["available"] == 0
    assert closed["percentage"] == 0
    assert closed["health"] == "critical"
    # A closed backend is the cluster's bottleneck.
    assert health["bottleneck"] == "htcondorcern"
    assert health["percentage"] == 0


def test_workflow_health_bottleneck_is_the_saturated_backend(workflow_health):
    """A saturated external backend becomes the bottleneck over a roomy cap."""
    health = workflow_health(
        {"kubernetes": 30, "htcondorcern": 5, "dask": 5},
        {"kubernetes": 2, "htcondorcern": 5},
    )

    assert health["backends"]["kubernetes"]["percentage"] == 93
    assert health["backends"]["htcondorcern"]["percentage"] == 0
    assert health["bottleneck"] == "htcondorcern"
    # The top-level tile mirrors the bottleneck, so the dashboard reports the
    # same "cluster full" verdict the scheduler would reach.
    assert health["used"] == 5
    assert health["total"] == 5
    assert health["available"] == 0
    assert health["percentage"] == 0
    assert health["health"] == "critical"


def test_workflow_health_hides_dask_when_disabled(workflow_health):
    """No idle Dask tile is advertised on a cluster that rejects Dask workflows."""
    health = workflow_health(
        {"kubernetes": 30, "dask": 5}, {"kubernetes": 1}, dask_enabled=False
    )

    assert "dask" not in health["backends"]
    assert health["bottleneck"] == "kubernetes"


def test_workflow_health_shows_dask_usage_after_disabling(workflow_health):
    """Workflows still running from before Dask was disabled stay visible."""
    health = workflow_health(
        {"kubernetes": 30, "dask": 5},
        {"kubernetes": 1, "dask": 2},
        dask_enabled=False,
    )

    assert health["backends"]["dask"]["used"] == 2


def test_workflow_health_shows_dask_when_enabled(workflow_health):
    """An enabled but idle Dask cap is reported so operators see the headroom."""
    health = workflow_health({"kubernetes": 30, "dask": 5}, {"kubernetes": 1})

    assert health["backends"]["dask"] == {
        "used": 0,
        "total": 5,
        "available": 5,
        "percentage": 100,
        "health": "healthy",
    }
