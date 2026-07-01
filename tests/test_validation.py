# This file is part of REANA.
# Copyright (C) 2022, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server tests for validation module."""

import io

import pytest
from unittest.mock import patch
from contextlib import nullcontext as does_not_raise

from flask import Flask
from werkzeug.datastructures import FileStorage

from reana_commons.config import REANA_DEFAULT_SNAKEMAKE_ENV_IMAGE
from reana_commons.errors import REANAValidationError

# The per-check server wrappers (``validate_inputs``/``validate_images``) have
# been removed: every server path now validates through the single shared
# validator (``validate_serialized_spec``). These low-level checks live in
# reana-commons, so the remaining unit tests target them there directly.
from reana_commons.validation.images import validate_images
from reana_commons.validation.utils import MAX_LOAD_ERROR_MESSAGE_CHARS, validate_inputs
from reana_server.rest import workflows

import reana_server.validation as server_validation
from reana_server.validation import (
    SpecValidationServiceError,
    _authoritative_report,
    _call_rwc_validate,
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


def test_spec_bundle_request_size_rejected_before_multipart_parsing(monkeypatch):
    """Oversized bundle requests are rejected using Content-Length."""
    monkeypatch.setattr(workflows, "REANA_SPEC_BUNDLE_MAX_BYTES", 10)
    app = Flask(__name__)
    with app.test_request_context(
        "/api/workflows/validate",
        method="POST",
        data=b"01234567890",
        content_type="multipart/form-data",
    ):
        with pytest.raises(REANAValidationError, match="too large"):
            workflows._validate_spec_bundle_request_size()


def _uploaded_file(content=b"content", filename="file.txt"):
    """Build a small in-memory uploaded file."""
    return FileStorage(stream=io.BytesIO(content), filename=filename)


def test_stage_validation_bundle_rejects_too_many_files(monkeypatch, tmp_path):
    """Bundle staging is bounded by the configured file-count limit."""
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "REANA_SPEC_BUNDLE_MAX_FILES", 1)

    with pytest.raises(REANAValidationError, match="too many files"):
        workflows._stage_validation_bundle(
            {"reana.yaml": _uploaded_file(), "Snakefile": _uploaded_file()}
        )


def test_stage_validation_bundle_rejects_unsafe_member_and_cleans_up(
    monkeypatch, tmp_path
):
    """Unsafe relative paths are rejected and partial staging is removed."""
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))

    with pytest.raises(REANAValidationError, match="Unsafe bundle path"):
        workflows._stage_validation_bundle(
            {"reana.yaml": _uploaded_file(), "../escape": _uploaded_file()}
        )

    assert not list((tmp_path / workflows.VALIDATION_STAGING_SUBDIR).glob("*"))


def test_stage_validation_bundle_enforces_size_cap_while_streaming(
    monkeypatch, tmp_path
):
    """An oversized member is rejected mid-stream, even with no Content-Length.

    The cap is enforced while streaming each member to disk, so a chunked upload
    (which bypasses the up-front Content-Length check) cannot land a huge file.
    """
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "REANA_SPEC_BUNDLE_MAX_BYTES", 8)

    with pytest.raises(REANAValidationError, match="too large"):
        workflows._stage_validation_bundle(
            {"reana.yaml": _uploaded_file(content=b"x" * 4096)}
        )

    assert not list((tmp_path / workflows.VALIDATION_STAGING_SUBDIR).glob("*"))


# --- SNDBX-03 / SNDBX-07: server-side re-validation + error taxonomy ---------


def _candidate_serial_spec(image):
    """A minimal already-serialized serial spec usable as a sandbox candidate."""
    return {
        "workflow": {
            "type": "serial",
            "specification": {
                "steps": [{"name": "s", "environment": image, "commands": ["echo hi"]}]
            },
        },
        "inputs": {"parameters": {}},
    }


def test_authoritative_report_validates_loader_candidate_against_policy():
    """The server validates the sandbox's candidate spec itself (loader-only).

    The sandbox emits only the loaded spec (no verdict); the server runs the
    pure policy validator on it and decides. A non-vetted image is rejected.
    """
    candidate = _candidate_serial_spec("evil.io/malware:latest")
    loader_report = {"reana_specification": candidate, "error": None}
    policy = {
        "vetted_images_enabled": True,
        "vetted_images_allowlist": ["docker.io/library/busybox:1.36"],
    }
    result = _authoritative_report(loader_report, exit_code=0, policy=policy)
    assert result["valid"] is False
    assert any(e["code"] == "image_not_allowed" for e in result["errors"])


def test_authoritative_report_valid_candidate_passes():
    """A loaded spec that meets policy is reported valid, keeping the spec."""
    candidate = _candidate_serial_spec("docker.io/library/busybox:1.36")
    loader_report = {"reana_specification": candidate, "error": None}
    policy = {
        "vetted_images_enabled": True,
        "vetted_images_allowlist": ["docker.io/library/busybox:1.36"],
    }
    result = _authoritative_report(loader_report, exit_code=0, policy=policy)
    assert result["valid"] is True
    assert result["reana_specification"] == candidate


def test_authoritative_report_internal_exit_code_is_service_error():
    """Sandbox exit code 2 (internal error) is a service failure, not invalid."""
    report = {"reana_specification": None, "error": None}
    with pytest.raises(SpecValidationServiceError):
        _authoritative_report(report, exit_code=2, policy={})


def test_authoritative_report_internal_coded_error_is_service_error():
    """An ``internal``-coded error is a service failure regardless of exit code."""
    report = {
        "reana_specification": None,
        "error": {"code": "internal", "message": "boom"},
    }
    with pytest.raises(SpecValidationServiceError, match="boom"):
        _authoritative_report(report, exit_code=None, policy={})


def test_authoritative_report_load_failure_is_invalid_not_service_error():
    """A spec that fails to load is a user-facing invalid result (not a 500)."""
    report = {
        "reana_specification": None,
        "error": {"code": "load", "message": "bad Snakefile"},
    }
    result = _authoritative_report(report, exit_code=1, policy={})
    assert result["valid"] is False
    assert result["reana_specification"] is None
    assert result["errors"][0]["code"] == "load"
    assert result["errors"][0]["message"] == "bad Snakefile"


def test_authoritative_report_load_message_is_bounded():
    """A huge/multi-line loader message is reduced to a bounded first line."""
    report = {
        "reana_specification": None,
        "error": {"code": "load", "message": "X" * 5000 + "\nsecond line"},
    }
    message = _authoritative_report(report, exit_code=1, policy={})["errors"][0][
        "message"
    ]
    assert len(message) == MAX_LOAD_ERROR_MESSAGE_CHARS + len("...")
    assert message.endswith("...")
    assert "second line" not in message


def test_validate_spec_bundle_serial_load_failure_is_invalid(monkeypatch, tmp_path):
    """A serial spec that fails to load is invalid (not a 500) -- sandbox parity.

    The serial branch loads in-process; a load failure must yield the same
    ``code == "load"`` invalid report (bounded message) as the sandbox path,
    rather than propagating as an unhandled 500.
    """
    (tmp_path / "reana.yaml").write_text("version: 0.3.0\n")
    monkeypatch.setattr(server_validation, "build_validation_policy", lambda: {})

    def _boom(*args, **kwargs):
        raise RuntimeError(
            "[Errno 2] No such file or directory: 'code/helloworld.py'\nframe"
        )

    monkeypatch.setattr("reana_commons.specification.load_reana_spec", _boom)

    report = server_validation.validate_spec_bundle(
        str(tmp_path), "validation-tmp/x", "serial"
    )
    assert report["valid"] is False
    assert report["reana_specification"] is None
    assert report["errors"][0]["code"] == "load"
    assert report["errors"][0]["message"] == (
        "[Errno 2] No such file or directory: 'code/helloworld.py'"
    )


def test_call_rwc_validate_transport_error_is_service_error(monkeypatch):
    """A controller transport failure surfaces as a service error (-> 500)."""

    def _boom(*args, **kwargs):
        raise server_validation.requests.exceptions.RequestException("unreachable")

    monkeypatch.setattr(server_validation.requests, "post", _boom)
    with pytest.raises(SpecValidationServiceError):
        _call_rwc_validate("validation-tmp/x")


def test_call_rwc_validate_controller_error_is_service_error(monkeypatch):
    """A non-OK controller response surfaces as a service error (-> 500)."""

    class _Resp:
        ok = False

        def json(self):
            return {"message": "controller exploded"}

    monkeypatch.setattr(server_validation.requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(SpecValidationServiceError, match="controller exploded"):
        _call_rwc_validate("validation-tmp/x")


ALLOWLIST = {
    "enabled": True,
    "allowlist": ["docker.io/reanahub/reana-env-root6:6.18.04"],
}
DISALLOWED_IMAGE = "docker.io/bitcoin-miner:1.2.3"
ALLOWED_IMAGE = "docker.io/reanahub/reana-env-root6:6.18.04"


def serial_workflow(*images):
    return {
        "type": "serial",
        "specification": {"steps": [{"environment": img} for img in images]},
    }


def snakemake_workflow(*images):
    return {
        "type": "snakemake",
        "specification": {"steps": [{"environment": img} for img in images]},
    }


@pytest.mark.parametrize(
    "config, workflow, error",
    [
        pytest.param(
            {"enabled": False, "allowlist": []},
            serial_workflow(DISALLOWED_IMAGE),
            does_not_raise(),
            id="disabled-anything-goes",
        ),
        # Serial: explicit environment field
        pytest.param(
            ALLOWLIST,
            serial_workflow(ALLOWED_IMAGE),
            does_not_raise(),
            id="serial-allowed",
        ),
        pytest.param(
            ALLOWLIST,
            serial_workflow(DISALLOWED_IMAGE),
            pytest.raises(REANAValidationError, match="not allowed"),
            id="serial-disallowed",
        ),
        pytest.param(
            ALLOWLIST,
            serial_workflow(ALLOWED_IMAGE, DISALLOWED_IMAGE),
            pytest.raises(REANAValidationError, match="not allowed"),
            id="serial-mixed",
        ),
        # Snakemake: rules with explicit container directive
        pytest.param(
            ALLOWLIST,
            snakemake_workflow(ALLOWED_IMAGE),
            does_not_raise(),
            id="snakemake-explicit-container-allowed",
        ),
        pytest.param(
            ALLOWLIST,
            snakemake_workflow(DISALLOWED_IMAGE),
            pytest.raises(REANAValidationError, match="not allowed"),
            id="snakemake-explicit-container-disallowed",
        ),
        # Snakemake: the runtime default must be explicitly allowlisted.
        pytest.param(
            {"enabled": True, "allowlist": []},
            snakemake_workflow(""),
            pytest.raises(REANAValidationError, match="not allowed"),
            id="snakemake-no-container-empty-allowlist-rejected",
        ),
        pytest.param(
            {
                "enabled": True,
                "allowlist": [REANA_DEFAULT_SNAKEMAKE_ENV_IMAGE],
            },
            snakemake_workflow(""),
            does_not_raise(),
            id="snakemake-no-container-default-allowlisted",
        ),
        pytest.param(
            {
                "enabled": True,
                "allowlist": [
                    ALLOWED_IMAGE,
                    REANA_DEFAULT_SNAKEMAKE_ENV_IMAGE,
                ],
            },
            snakemake_workflow(ALLOWED_IMAGE, ""),
            does_not_raise(),
            id="snakemake-mixed-with-and-without-container",
        ),
        pytest.param(
            ALLOWLIST,
            snakemake_workflow(DISALLOWED_IMAGE, ""),
            pytest.raises(REANAValidationError, match="not allowed"),
            id="snakemake-mixed-disallowed-explicit-plus-no-container",
        ),
        # CWL: images come from requirements[].dockerPull, not steps
        pytest.param(
            ALLOWLIST,
            {
                "type": "cwl",
                "specification": {
                    "$graph": [
                        {
                            "class": "Workflow",
                            "requirements": [
                                {
                                    "class": "DockerRequirement",
                                    "dockerPull": ALLOWED_IMAGE,
                                }
                            ],
                        }
                    ]
                },
            },
            does_not_raise(),
            id="cwl-allowed",
        ),
        pytest.param(
            ALLOWLIST,
            {
                "type": "cwl",
                "specification": {
                    "$graph": [
                        {
                            "class": "Workflow",
                            "requirements": [
                                {
                                    "class": "DockerRequirement",
                                    "dockerPull": DISALLOWED_IMAGE,
                                }
                            ],
                        }
                    ]
                },
            },
            pytest.raises(REANAValidationError, match="not allowed"),
            id="cwl-disallowed",
        ),
        pytest.param(
            {"enabled": True, "allowlist": []},
            {
                "type": "cwl",
                "specification": {
                    "$graph": [{"class": "Workflow", "requirements": []}]
                },
            },
            does_not_raise(),
            id="cwl-no-docker-requirement",
        ),
        # Yadage: images come from nested stages, not a flat steps list
        pytest.param(
            ALLOWLIST,
            {
                "type": "yadage",
                "specification": {
                    "stages": [
                        {
                            "name": "stage1",
                            "scheduler": {
                                "step": {
                                    "environment": {
                                        "environment_type": "docker-encapsulated",
                                        "image": "docker.io/reanahub/reana-env-root6",
                                        "imagetag": "6.18.04",
                                    }
                                }
                            },
                        }
                    ]
                },
            },
            does_not_raise(),
            id="yadage-allowed",
        ),
        pytest.param(
            ALLOWLIST,
            {
                "type": "yadage",
                "specification": {
                    "stages": [
                        {
                            "name": "stage1",
                            "scheduler": {
                                "step": {
                                    "environment": {
                                        "environment_type": "docker-encapsulated",
                                        "image": "docker.io/bitcoin-miner",
                                        "imagetag": "1.2.3",
                                    }
                                }
                            },
                        }
                    ]
                },
            },
            pytest.raises(REANAValidationError, match="not allowed"),
            id="yadage-disallowed",
        ),
        pytest.param(
            ALLOWLIST,
            {
                "type": "yadage",
                "specification": {
                    "stages": [
                        {
                            "name": "outer",
                            "scheduler": {
                                "workflow": {
                                    "stages": [
                                        {
                                            "name": "inner",
                                            "scheduler": {
                                                "step": {
                                                    "environment": {
                                                        "environment_type": "docker-encapsulated",
                                                        "image": "docker.io/bitcoin-miner",
                                                        "imagetag": "1.2.3",
                                                    }
                                                }
                                            },
                                        }
                                    ]
                                }
                            },
                        }
                    ]
                },
            },
            pytest.raises(REANAValidationError, match="not allowed"),
            id="yadage-nested-stage-disallowed",
        ),
        pytest.param(
            {"enabled": True, "allowlist": []},
            {"type": "yadage", "specification": {"stages": []}},
            does_not_raise(),
            id="yadage-no-stages",
        ),
    ],
)
def test_validate_images(config, workflow, error):
    with error:
        validate_images(
            {"workflow": workflow},
            enabled=config["enabled"],
            allowlist=config["allowlist"],
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
