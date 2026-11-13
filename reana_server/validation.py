# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022, 2023, 2024, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server validation utilities."""

import logging
import os
from typing import Dict, List, Optional, Tuple

import requests
import yaml

from reana_commons.config import (
    OPENAPI_SPECS,
    SHARED_VOLUME_PATH,
    WORKFLOW_RUNTIME_USER_GID,
    WORKFLOW_RUNTIME_USER_UID,
    WORKSPACE_PATHS,
)
from reana_commons.errors import REANAValidationError
from reana_commons.specification_paths import (
    uses_legacy_validation_scope,
    workflow_parameter_file,
)
from reana_commons.validation.environments import check_environment_tags
from reana_commons.validation.images import extract_image_environments, extract_images
from reana_commons.validation.report import (
    MAX_VALIDATION_REPORT_ENTRIES,
    validate_serialized_spec,
)
from reana_commons.validation.sandbox import (
    ERROR_CODE_INTERNAL,
    EXIT_INTERNAL_ERROR,
    EXIT_LOAD_ERROR,
    EXIT_LOADED,
    REANA_SPEC_FILENAMES,
)
from reana_commons.validation.utils import (
    bound_error_message,
    validate_retention_rule as _validate_retention_rule,
)

from reana_server.config import (
    REANA_SPEC_VALIDATION_TIMEOUT,
    SUPPORTED_COMPUTE_BACKENDS,
    WORKSPACE_RETENTION_PERIOD,
    DASK_ENABLED,
    REANA_DASK_CLUSTER_MAX_MEMORY_LIMIT,
    REANA_DASK_CLUSTER_DEFAULT_NUMBER_OF_WORKERS,
    REANA_DASK_CLUSTER_MAX_NUMBER_OF_WORKERS,
    REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_MEMORY,
    REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_MEMORY,
    REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_THREADS,
    REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_THREADS,
    REANA_VETTED_CONTAINER_IMAGES,
)


def validate_input_parameters(
    input_parameters: Dict, original_parameters: Dict
) -> Dict:
    """Validate input parameters.

    :param input_parameters: dictionary which represents additional workflow input parameters.
    :param original_parameters: dictionary which represents original workflow input parameters.

    :raises REANAValidationError: Given there are additional input parameters which are not present in the REANA spec parameter list.
    """
    for parameter in input_parameters.keys():
        if parameter not in original_parameters:
            raise REANAValidationError(
                f'Input parameter "{parameter}" is not present in reana.yaml'
            )
    return input_parameters


def validate_loaded_spec(reana_spec: Dict) -> List[Dict]:
    """Validate an already fully-loaded REANA specification.

    This is the thin in-process counterpart to :func:`load_and_validate_spec`
    for paths that already hold a *trusted*, fully-loaded specification (workflow
    loaded + input parameters resolved) and therefore do not need to load any
    untrusted bundle -- e.g. clone/restart from the stored specification. It runs
    the single shared validator
    (:func:`reana_commons.validation.report.validate_serialized_spec`) against the
    cluster validation policy.

    :param reana_spec: A fully-loaded REANA specification dictionary.
    :returns: The list of advisory validation warnings.
    :raises reana_commons.errors.REANAValidationError: if the specification is
        invalid.
    """
    report = validate_serialized_spec(reana_spec, build_validation_policy())
    if not report["valid"]:
        message = "; ".join(e.get("message", "") for e in report.get("errors", []))
        raise REANAValidationError(message or "Invalid workflow specification.")
    return report.get("warnings", [])


def validate_retention_rule(rule: str, days: int) -> None:
    """Validate retention rule.

    :param rule: retention rule
    :type rule: str

    :param days: after how many days rules need to be applied
    :type days: int

    :raises reana_commons.errors.REANAValidationError: if rule is not valid
    """
    _validate_retention_rule(
        rule, days, max_retention_period=WORKSPACE_RETENTION_PERIOD
    )


def list_spec_image_environments(reana_spec: Dict) -> List[Dict]:
    """Return distinct runtime image and effective UID/GID combinations."""
    try:
        return extract_image_environments(
            reana_spec,
            int(WORKFLOW_RUNTIME_USER_UID),
            int(WORKFLOW_RUNTIME_USER_GID),
        )
    except Exception as e:
        logging.warning(
            "Could not extract image runtime identities from the specification: %s",
            e,
        )
        return []


def check_spec_environment_tags(reana_spec: Dict) -> List[Dict]:
    """Return offline image-tag warnings for a loaded specification.

    Returns advisory warning entries (``{code, message, path}``) ready to append
    to a validation report's ``warnings``. Registry availability and image
    filesystem checks run only through the client's explicit ``--pull`` option.
    """
    try:
        images = extract_images(reana_spec)
    except Exception as e:
        logging.warning("Could not extract images for environment check: %s", e)
        return []
    findings = check_environment_tags(images)
    return [
        {"code": f["code"], "message": f["message"], "path": f["image"]}
        for f in findings
    ]


# ---------------------------------------------------------------------------
# Server-side spec loading + validation orchestration
#
# These power the "raw spec bundle" flow used by thin clients (e.g. the Go
# client) that cannot run the workflow engines themselves. The bundle (raw
# reana.yaml + referenced workflow/config files) is staged on the shared volume;
# serial specs are loaded + validated in-process (loading serial is pure), while
# Snakemake/CWL/Yadage specs -- whose loading executes untrusted code -- are sent
# to reana-workflow-controller, which runs the sandboxed validator Job.
# ---------------------------------------------------------------------------


def build_validation_policy() -> Dict:
    """Build the cluster validation policy passed to the shared validator.

    The same dict shape is consumed by
    :func:`reana_commons.validation.report.validate_serialized_spec` (in-process,
    for serial) and injected into the sandbox Job (for non-serial).
    """
    return {
        "vetted_images_enabled": REANA_VETTED_CONTAINER_IMAGES["enabled"],
        "vetted_images_allowlist": list(REANA_VETTED_CONTAINER_IMAGES["allowlist"]),
        "supported_backends": SUPPORTED_COMPUTE_BACKENDS,
        "workspace_paths": list(WORKSPACE_PATHS.values()),
        "dask_config": {
            "enabled": DASK_ENABLED,
            "max_memory_limit": REANA_DASK_CLUSTER_MAX_MEMORY_LIMIT,
            "default_number_of_workers": REANA_DASK_CLUSTER_DEFAULT_NUMBER_OF_WORKERS,
            "max_number_of_workers": REANA_DASK_CLUSTER_MAX_NUMBER_OF_WORKERS,
            "default_single_worker_memory": REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_MEMORY,
            "max_single_worker_memory": REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_MEMORY,
            "default_single_worker_threads": REANA_DASK_CLUSTER_DEFAULT_SINGLE_WORKER_THREADS,
            "max_single_worker_threads": REANA_DASK_CLUSTER_MAX_SINGLE_WORKER_THREADS,
        },
        "max_retention_period": WORKSPACE_RETENTION_PERIOD,
    }


def _find_reana_yaml(bundle_dir: str) -> str:
    """Return the path to the REANA specification file in a bundle directory."""
    for name in REANA_SPEC_FILENAMES:
        candidate = os.path.join(bundle_dir, name)
        if os.path.isfile(candidate):
            return candidate
    raise REANAValidationError(
        "No REANA specification file ({}) found in the uploaded bundle.".format(
            " or ".join(REANA_SPEC_FILENAMES)
        )
    )


def has_reana_spec_file(workspace_path: str) -> bool:
    """Whether a workspace directory contains a ``reana.yaml``/``reana.yml``.

    Used by the start gate to decide between re-loading the workspace
    (workspace-authoritative) and falling back to the stored specification:
    launched workflows have their spec file stripped by ``filter_input_files``
    and legacy (pre-seeding) workflows never had one seeded.
    """
    return any(
        os.path.lexists(os.path.join(workspace_path, name))
        for name in REANA_SPEC_FILENAMES
    )


class SpecValidationServiceError(Exception):
    """The validation *service* failed -- the validator could not run.

    Distinct from an invalid specification: this means the sandbox/controller
    could not do its job (controller unreachable, controller 5xx, or the sandbox
    exited with the internal-error code 2), so it must surface as a server-side
    error (HTTP 500), not a "bad specification" (HTTP 400).
    """


def _call_rwc_validate(
    bundle_rel_path: str, timeout: Optional[int] = None
) -> Tuple[Optional[int], Dict]:
    """Ask reana-workflow-controller to load a spec in the sandbox.

    The sandbox is a pure loader and applies no policy, so none is sent; the
    server validates the returned specification itself.

    :param bundle_rel_path: Bundle path relative to the shared volume root.
    :returns: ``(exit_code, report)`` from the sandbox, where ``report`` is
        ``{reana_specification, error}``.
    :raises SpecValidationServiceError: if the validation service itself fails
        (controller unreachable or a non-OK controller response). This is a
        service outage, not an invalid specification.
    """
    rwc_url = OPENAPI_SPECS["reana-workflow-controller"][0]
    try:
        response = requests.post(
            "{}/api/workflows/validate".format(rwc_url),
            json={
                "bundle_path": bundle_rel_path,
                "timeout": timeout,
            },
            # Safety net so a stuck controller cannot hang the request forever;
            # the real bound is the sandbox Job's activeDeadlineSeconds. The read
            # timeout is derived from REANA_SPEC_VALIDATION_TIMEOUT (the shared
            # sandbox deadline) with a buffer above the controller's own
            # ``timeout + 60`` wait, so the server never gives up first.
            timeout=(10, REANA_SPEC_VALIDATION_TIMEOUT + 120),
        )
    except requests.exceptions.RequestException as e:
        logging.error("Could not reach the spec validation service: %s", e)
        raise SpecValidationServiceError(
            "Could not reach the workflow specification validation service."
        )
    if not response.ok:
        message = "Workflow specification validation service error."
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = payload.get("message", message)
        except ValueError:
            pass
        raise SpecValidationServiceError(message)
    # A 200 with a non-JSON body or without a ``report`` key is still a service
    # failure (a misbehaving controller), not an invalid specification: classify
    # it as such so callers surface a clean 500 rather than an opaque one.
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if (
        not isinstance(payload, dict)
        or "report" not in payload
        or not isinstance(payload["report"], dict)
    ):
        raise SpecValidationServiceError(
            "Workflow specification validation service returned a malformed response."
        )
    return payload.get("exit_code"), payload["report"]


def _load_error_report(message: str) -> Dict:
    """Build the standard "specification could not be loaded" invalid report.

    Shared by the in-process serial path and the sandbox path so a spec that
    fails to load produces an identical structured (``code == "load"``) report --
    and hence the same HTTP 400 + message -- regardless of engine.
    """
    return {
        "valid": False,
        "reana_specification": None,
        "errors": [{"code": "load", "message": message, "path": ""}],
        "warnings": [],
    }


def load_raw_spec_mapping(bundle_dir: str) -> Dict:
    """Safely load the bundle's top-level REANA YAML mapping.

    This preliminary parse is used only to discover the workflow type and to
    bind restart payloads to the canonical workspace specification. YAML syntax
    failures and non-mapping top-level values are ordinary user specification
    errors, never validation-service failures.

    :raises REANAValidationError: if the YAML is malformed or not a mapping.
    """
    reana_yaml_path = _find_reana_yaml(bundle_dir)
    try:
        with open(reana_yaml_path) as spec_file:
            raw_yaml = yaml.safe_load(spec_file)
    except (OSError, yaml.YAMLError) as e:
        raise REANAValidationError(
            "Could not load the REANA specification YAML: {}".format(
                bound_error_message(e)
            )
        )
    if not isinstance(raw_yaml, dict):
        raise REANAValidationError(
            "The REANA specification must be a YAML mapping at the top level."
        )
    workflow = raw_yaml.get("workflow")
    if workflow is not None and not isinstance(workflow, dict):
        raise REANAValidationError(
            "The REANA specification 'workflow' field must be a mapping."
        )
    return raw_yaml


def _authoritative_report(report: Dict, exit_code: Optional[int], policy: Dict) -> Dict:
    """Decide valid/invalid server-side from a sandbox *loader* report.

    The sandbox only loads (running untrusted code) and returns the serialized
    ``reana_specification`` -- it computes no verdict, so there is nothing to
    trust or forge. We run the *pure*, code-execution-free policy validator
    (:func:`reana_commons.validation.report.validate_serialized_spec`) on that
    candidate in-process; that in-process result is the authoritative decision.

    The sandbox report is ``{reana_specification, error}`` and the exit code
    classifies the loading outcome (0 loaded, 1 load error, 2 internal).

    :raises SpecValidationServiceError: on an infrastructure failure (sandbox
        exit code 2 or an ``internal``-coded error), which is not a spec problem.
    """
    error = report.get("error") or {}
    candidate = report.get("reana_specification")

    if error.get("code") == ERROR_CODE_INTERNAL:
        raise SpecValidationServiceError(
            error.get("message") or "internal validation error"
        )

    if type(exit_code) is not int or exit_code not in (
        EXIT_LOADED,
        EXIT_LOAD_ERROR,
        EXIT_INTERNAL_ERROR,
    ):
        raise SpecValidationServiceError(
            "Unexpected workflow specification validator exit code."
        )

    if exit_code == EXIT_INTERNAL_ERROR:
        raise SpecValidationServiceError(
            error.get("message") or "internal validation error"
        )

    if exit_code == EXIT_LOAD_ERROR:
        if candidate:
            raise SpecValidationServiceError(
                "Workflow specification validator returned a candidate for a "
                "failed load."
            )
        # The specification could not be loaded at all (e.g. a Snakefile that
        # fails to parse). That is a user-facing validation error, not a service
        # failure -- report it as invalid, surfacing the loader message. The
        # sandbox already bounds it, but re-bound here defensively: the message
        # comes from untrusted-code execution and the server never trusts the
        # sandbox to have kept it small.
        return _load_error_report(bound_error_message(error.get("message") or ""))

    if not candidate:
        raise SpecValidationServiceError(
            "Workflow specification validator reported a successful load without "
            "a candidate specification."
        )

    # Authoritative validation in-process (pure, no code execution).
    authoritative = validate_serialized_spec(candidate, policy)
    authoritative["reana_specification"] = candidate
    return authoritative


def _add_legacy_scope_warnings(report, legacy_parameter_file, legacy_scope):
    """Add bounded migration warnings for legacy validation declarations."""
    warnings = report.setdefault("warnings", [])
    findings = []
    if legacy_parameter_file:
        findings.append(
            {
                "code": "deprecated_parameters_input",
                "message": (
                    "inputs.parameters.input is deprecated; use "
                    "workflow.parameters.file instead."
                ),
                "path": "inputs.parameters.input",
            }
        )
    if legacy_scope:
        findings.append(
            {
                "code": "deprecated_input_workflow_sources",
                "message": (
                    "Legacy inputs.files or inputs.directories were included in "
                    "validation; declare loader dependencies under workflow.files "
                    "or workflow.directories instead."
                ),
                "path": "inputs",
            }
        )
    for finding in findings:
        if len(report.get("errors", [])) + len(warnings) >= (
            MAX_VALIDATION_REPORT_ENTRIES
        ):
            break
        warnings.append(finding)


def validate_spec_bundle(bundle_dir: str, bundle_rel_path: str) -> Dict:
    """Load and validate a raw spec bundle, returning a structured report.

    Loading is done in-process for serial specs (pure dictionary manipulation)
    and in the sandboxed loader Job spawned by reana-workflow-controller for
    Snakemake/CWL/Yadage (because it executes untrusted code). In *both* cases
    the policy validation is applied in-process here -- the sandbox only loads;
    see :func:`_authoritative_report`.

    :param bundle_dir: Absolute path of the staged bundle on the shared volume.
    :param bundle_rel_path: Bundle path relative to the shared volume root.
    :returns: Report dict ``{valid, reana_specification, errors, warnings}``.
    :raises SpecValidationServiceError: if the validation service itself failed.
    """
    try:
        raw_yaml = load_raw_spec_mapping(bundle_dir)
    except REANAValidationError as e:
        return _load_error_report(bound_error_message(e))
    try:
        _parameter_file, legacy_parameter_file = workflow_parameter_file(raw_yaml)
    except REANAValidationError as e:
        return _load_error_report(bound_error_message(e))

    workflow = raw_yaml.get("workflow") or {}
    workflow_type = workflow.get("type") if isinstance(workflow, dict) else None
    legacy_scope = uses_legacy_validation_scope(raw_yaml)
    policy = build_validation_policy()

    if workflow_type == "serial":
        # Safe to do in-process: serial loading does not execute user code.
        from reana_commons.specification import load_reana_spec

        try:
            reana_yaml = load_reana_spec(
                _find_reana_yaml(bundle_dir), workspace_path=bundle_dir
            )
        except Exception as e:
            # A serial spec that fails to load is a user-facing validation error,
            # not a service failure -- report it as invalid with the same bounded
            # "load" message shape as the sandbox path (rather than letting it
            # bubble up as a 500).
            return _load_error_report(bound_error_message(e))
        report = validate_serialized_spec(reana_yaml, policy)
        report["reana_specification"] = reana_yaml
        _add_legacy_scope_warnings(report, legacy_parameter_file, legacy_scope)
        return report

    exit_code, report = _call_rwc_validate(bundle_rel_path)
    authoritative = _authoritative_report(report, exit_code, policy)
    _add_legacy_scope_warnings(authoritative, legacy_parameter_file, legacy_scope)
    return authoritative


def load_and_validate_spec(bundle_dir: str) -> Tuple[Dict, List[Dict]]:
    """Load and validate a raw spec bundle, returning the authoritative spec.

    This is the single chokepoint for every server-side path that must load an
    *untrusted* workflow specification (create, launch, GitLab, start, restart).
    Loading is done in-process for serial workflows and inside the sandboxed
    validator Job for Snakemake/CWL/Yadage, so the API process never executes
    untrusted workflow code. The valid/invalid decision is always made
    server-side (the sandbox is only a loader). Invalid specs raise immediately
    (fail early).

    :param bundle_dir: Absolute path of the staged bundle on the shared volume.
    :returns: ``(reana_specification, warnings)`` where ``reana_specification``
        is the fully loaded spec (workflow loaded + input parameters resolved).
    :raises REANAValidationError: if the bundle is missing a reana.yaml or the
        specification is invalid.
    :raises SpecValidationServiceError: if the validation service itself failed
        (the validator could not run, as opposed to the spec being invalid).
    """
    bundle_rel_path = os.path.relpath(bundle_dir, SHARED_VOLUME_PATH)
    report = validate_spec_bundle(bundle_dir, bundle_rel_path)

    if not report.get("valid"):
        message = "; ".join(e.get("message", "") for e in report.get("errors", []))
        raise REANAValidationError(message or "Invalid workflow specification.")

    return report["reana_specification"], report.get("warnings", [])
