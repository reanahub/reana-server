# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Regression tests for restarting from current workspace sources."""

import copy
import json
import os
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from flask import url_for

from reana_commons.specification import load_reana_spec
from reana_db.models import RunStatus, Workflow


@pytest.fixture
def restart_workspace(app, monkeypatch, sample_serial_workflow_in_db, session):
    """Provide a completed workflow and emulate sandbox loading with real engines."""
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.finished
    session.commit()
    shared = app.config["SHARED_VOLUME_PATH"]
    monkeypatch.setattr("reana_server.rest.workflows.SHARED_VOLUME_PATH", shared)
    monkeypatch.setattr("reana_server.validation.SHARED_VOLUME_PATH", shared)

    def sandbox_load(relative_path):
        directory = os.path.join(shared, relative_path)
        try:
            loaded = load_reana_spec(
                os.path.join(directory, "reana.yaml"), workspace_path=directory
            )
        except Exception as error:
            return 1, {
                "reana_specification": None,
                "error": {"code": "load", "message": str(error)},
            }
        return 0, {"reana_specification": loaded, "error": None}

    monkeypatch.setattr("reana_server.validation._call_rwc_validate", sandbox_load)
    with patch(
        "reana_server.rest.workflows.publish_workflow_submission"
    ) as publish, patch(
        "reana_server.rest.workflows._recalculate_shared_workspace_quota"
    ):
        yield workflow, Path(workflow.workspace_path), publish


def write_sources(directory, engine, legacy=False):
    """Write a small runnable definition with an external parameter file."""
    specification = {"workflow": {"type": engine, "files": []}, "inputs": {}}
    (directory / "params.yaml").write_text("events: 20000\n")
    if engine == "snakemake":
        specification["workflow"]["file"] = "Snakefile"
        (directory / "Snakefile").write_text(
            "rule output:\n"
            '    output: "result.txt"\n'
            '    params: events=config["events"]\n'
            '    container: "busybox:1.36"\n'
            '    shell: "echo old {params.events} > {output}"\n'
        )
    elif engine == "cwl":
        specification["workflow"]["file"] = "workflow.cwl"
        (directory / "workflow.cwl").write_text(
            "cwlVersion: v1.0\nclass: CommandLineTool\n"
            "baseCommand: echo\narguments: [old]\n"
            "inputs:\n  events: int\noutputs: []\n"
            "requirements:\n  DockerRequirement:\n    dockerPull: busybox:1.36\n"
        )
    else:
        specification["workflow"]["file"] = "workflow.yaml"
        specification["workflow"]["files"] = ["step.yaml"]
        specification["inputs"]["options"] = {"initfiles": ["params.yaml"]}
        (directory / "workflow.yaml").write_text(
            "stages:\n- name: output\n  dependencies: [init]\n"
            "  scheduler:\n    scheduler_type: singlestep-stage\n"
            "    parameters:\n      events: {step: init, output: events}\n"
            "    step: {$ref: step.yaml}\n"
        )
        (directory / "step.yaml").write_text(
            "process:\n  process_type: string-interpolated-cmd\n"
            "  cmd: echo old {events}\n"
            "publisher:\n  publisher_type: interpolated-pub\n  publish: {}\n"
            "environment:\n  environment_type: docker-encapsulated\n"
            "  image: busybox\n  imagetag: '1.36'\n"
        )
    if engine != "yadage":
        if legacy:
            specification["inputs"]["parameters"] = {"input": "params.yaml"}
        else:
            specification["workflow"]["parameters"] = {"file": "params.yaml"}
    (directory / "reana.yaml").write_text(yaml.safe_dump(specification))
    return specification


def post_restart(app, user, auth_headers, workflow, **parameters):
    """Submit a plain restart through the public API."""
    with app.test_client() as client:
        return client.post(
            url_for("workflows.start_workflow", workflow_id_or_name=str(workflow.id_)),
            headers=auth_headers(user),
            json=dict(parameters, restart=True),
        )


@pytest.mark.parametrize(
    "engine,legacy",
    [
        ("snakemake", False),
        ("snakemake", True),
        ("cwl", False),
        ("cwl", True),
        ("yadage", False),
    ],
)
@pytest.mark.parametrize("override", [False, True])
def test_restart_reloads_definitions_and_parameters(
    app, session, user0, auth_headers, restart_workspace, engine, legacy, override
):
    """Uploaded definitions and parameters replace the prior run's packed values."""
    workflow, directory, publish = restart_workspace
    raw = write_sources(directory, engine, legacy)
    workflow.type_ = engine
    workflow.reana_specification = load_reana_spec(
        str(directory / "reana.yaml"), str(directory)
    )
    session.commit()
    old_specification = copy.deepcopy(workflow.reana_specification)
    raw_bytes = (directory / "reana.yaml").read_bytes()
    definition = directory / (
        "step.yaml" if engine == "yadage" else raw["workflow"]["file"]
    )
    definition.write_text(definition.read_text().replace("old", "updated"))
    (directory / "params.yaml").write_text("events: 2000\n")

    response = post_restart(
        app,
        user0,
        auth_headers,
        workflow,
        input_parameters={"events": 500} if override else {},
    )

    assert response.status_code == 200, response.json
    cloned, _, submitted = publish.call_args.args
    assert cloned.id_ != workflow.id_
    assert cloned.reana_specification["inputs"]["parameters"]["events"] == (
        500 if override else 2000
    )
    assert "updated" in str(cloned.reana_specification["workflow"]["specification"])
    if engine == "snakemake":
        assert cloned.reana_specification["workflow"]["specification"]["steps"][0][
            "params"
        ]["events"] == (500 if override else 2000)
    assert submitted["input_parameters"] == ({"events": 500} if override else {})
    assert submitted["operational_options"] == raw["inputs"].get("options", {})
    assert workflow.reana_specification == old_specification
    assert (directory / "reana.yaml").read_bytes() == raw_bytes
    assert (directory / "params.yaml").read_text() == "events: 2000\n"
    assert not list(
        (Path(app.config["SHARED_VOLUME_PATH"]) / "validation-tmp").iterdir()
    )


@pytest.mark.parametrize("invalid", ["parameters", "definition", "missing", "override"])
def test_invalid_restart_does_not_clone_or_submit(
    app, session, user0, auth_headers, restart_workspace, invalid
):
    """Invalid updated sources or overrides fail before creating a new run."""
    workflow, directory, publish = restart_workspace
    workflow.reana_specification = write_sources(directory, "snakemake")
    workflow.type_ = "snakemake"
    session.commit()
    if invalid == "parameters":
        (directory / "params.yaml").write_text("- invalid\n")
    elif invalid == "definition":
        (directory / "Snakefile").write_text("this is invalid Python\n")
    elif invalid == "missing":
        (directory / "params.yaml").unlink()
    count = session.query(Workflow).count()

    response = post_restart(
        app,
        user0,
        auth_headers,
        workflow,
        input_parameters={"unknown": "value"} if invalid == "override" else {},
    )

    assert response.status_code == 400, response.json
    assert session.query(Workflow).count() == count
    assert workflow.status == RunStatus.finished
    publish.assert_not_called()


@pytest.mark.parametrize("engine", ["serial", "snakemake", "cwl", "yadage"])
def test_restart_without_raw_specification(
    app, session, user0, auth_headers, restart_workspace, engine
):
    """Only self-contained legacy serial specifications can safely be reused."""
    workflow, directory, publish = restart_workspace
    workflow.type_ = engine
    session.commit()
    for filename in ("reana.yaml", "reana.yml"):
        (directory / filename).unlink(missing_ok=True)

    response = post_restart(app, user0, auth_headers, workflow)

    if engine == "serial":
        assert response.status_code == 200, response.json
        publish.assert_called_once()
    else:
        assert response.status_code == 400
        assert "-f reana.yaml once" in response.json["message"]
        publish.assert_not_called()


def test_restart_validates_new_parameter_and_override_before_loading(
    app, session, user0, auth_headers, restart_workspace
):
    """New parameter names are accepted, and their overridden values are loaded."""
    workflow, directory, publish = restart_workspace
    write_sources(directory, "snakemake")
    workflow.type_ = "snakemake"
    workflow.reana_specification = load_reana_spec(
        str(directory / "reana.yaml"), str(directory)
    )
    session.commit()
    snakefile = directory / "Snakefile"
    snakefile.write_text(
        'assert int(config["new_events"]) > 0\n'
        + snakefile.read_text().replace('config["events"]', 'config["new_events"]')
    )
    (directory / "params.yaml").write_text("new_events: 2000\n")

    response = post_restart(
        app, user0, auth_headers, workflow, input_parameters={"new_events": -1}
    )
    assert response.status_code == 400, response.json
    publish.assert_not_called()

    response = post_restart(
        app, user0, auth_headers, workflow, input_parameters={"new_events": 500}
    )
    assert response.status_code == 200, response.json
    assert publish.call_args.args[0].reana_specification["inputs"]["parameters"] == {
        "new_events": 500
    }


def test_restart_yadage_overrides_initfiles_before_staging(
    app, session, user0, auth_headers, restart_workspace
):
    """An explicit initfiles override selects the files validated and executed."""
    workflow, directory, publish = restart_workspace
    workflow.reana_specification = write_sources(directory, "yadage")
    workflow.type_ = "yadage"
    session.commit()
    (directory / "replacement-params.yaml").write_text("events: 2000\n")
    (directory / "params.yaml").unlink()

    response = post_restart(
        app,
        user0,
        auth_headers,
        workflow,
        operational_options={"initfiles": ["replacement-params.yaml"]},
    )

    assert response.status_code == 200, response.json
    cloned, _, submitted = publish.call_args.args
    assert cloned.reana_specification["inputs"]["parameters"] == {"events": 2000}
    assert submitted["operational_options"] == {
        "initfiles": ["replacement-params.yaml"]
    }


@pytest.mark.parametrize("legacy", [False, True])
def test_restart_override_parameter_named_input(
    app, session, user0, auth_headers, restart_workspace, legacy
):
    """A runtime parameter named input must not replace a source file directive."""
    workflow, directory, publish = restart_workspace
    workflow.reana_specification = write_sources(directory, "snakemake", legacy)
    workflow.type_ = "snakemake"
    session.commit()
    contents = "events: 2000\ninput: original.txt\n"
    (directory / "params.yaml").write_text(contents)

    response = post_restart(
        app, user0, auth_headers, workflow, input_parameters={"input": "updated.txt"}
    )

    assert response.status_code == 200, response.json
    assert publish.call_args.args[0].reana_specification["inputs"]["parameters"] == {
        "events": 2000,
        "input": "updated.txt",
    }
    assert (directory / "params.yaml").read_text() == contents


def test_replacement_restart_keeps_overrides_out_of_workspace_yaml(
    app, session, user0, auth_headers, restart_workspace
):
    """Validate overrides, but retain the unmodified replacement as the source."""
    workflow, directory, publish = restart_workspace
    raw = write_sources(directory, "cwl")
    replacement = yaml.safe_dump(raw).encode()
    # A replacement may change engines; validate TARGET against CWL, not the
    # original serial workflow type.
    with app.test_client() as client:
        response = client.post(
            url_for(
                "workflows.restart_workflow", workflow_id_or_name=str(workflow.id_)
            ),
            headers=auth_headers(user0),
            data={
                "replacement": (BytesIO(replacement), "replacement.yaml"),
                "parameters": '{"input_parameters": {"events": 500}, "operational_options": {"TARGET": "result"}}',
            },
        )
    assert response.status_code == 200, response.json
    cloned, _, submitted = publish.call_args.args
    assert cloned.reana_specification["inputs"]["parameters"] == {"events": 500}
    assert submitted["operational_options"] == {"--target": "result"}
    assert (directory / "reana.yaml").read_bytes() == replacement


@pytest.mark.parametrize(
    "raw",
    [
        {"workflow": []},
        {"workflow": None},
        {"workflow": ""},
        {"workflow": {"type": "serial"}, "inputs": []},
        {"workflow": {"type": "serial"}, "inputs": {"options": []}},
    ],
)
@pytest.mark.parametrize("options", [{}, {"CACHE": "off"}])
@pytest.mark.parametrize("replacement", [False, True])
def test_restart_reports_invalid_mapping(
    app, user0, auth_headers, restart_workspace, raw, options, replacement
):
    """Malformed raw YAML is a client error on every restart submission path."""
    workflow, directory, publish = restart_workspace
    (directory / "reana.yaml").write_text(yaml.safe_dump(raw))
    if replacement:
        with app.test_client() as client:
            response = client.post(
                url_for(
                    "workflows.restart_workflow", workflow_id_or_name=str(workflow.id_)
                ),
                headers=auth_headers(user0),
                data={
                    "replacement": (
                        BytesIO(yaml.safe_dump(raw).encode()),
                        "reana.yaml",
                    ),
                    "parameters": json.dumps({"operational_options": options}),
                },
            )
    else:
        response = post_restart(
            app, user0, auth_headers, workflow, operational_options=options
        )
    assert response.status_code == 400, response.json
    publish.assert_not_called()


@pytest.mark.parametrize(
    "file_backed,raw_yaml,remove_yaml,replacement",
    [
        (False, False, False, False),
        (True, False, False, False),
        (False, True, False, False),
        (False, True, True, False),
        (False, True, True, True),
    ],
)
def test_serial_restart_overrides_are_run_only(
    app,
    session,
    user0,
    auth_headers,
    restart_workspace,
    file_backed,
    raw_yaml,
    remove_yaml,
    replacement,
):
    """Plain restarts retain defaults without inheriting previous run overrides."""
    workflow, directory, publish = restart_workspace
    baseline = {
        "workflow": {
            "type": "serial",
            "specification": {
                "steps": [
                    {
                        "name": "fit",
                        "environment": "busybox:1.36",
                        "commands": ["echo ${events}"],
                    }
                ]
            },
        },
        "inputs": {"parameters": {"events": 20000}, "options": {"CACHE": "on"}},
    }
    raw = copy.deepcopy(baseline)
    if file_backed:
        baseline["workflow"]["file"] = "workflow.json"
        raw["workflow"] = {"type": "serial", "file": "workflow.json"}
        (directory / "workflow.json").write_text(
            json.dumps(baseline["workflow"]["specification"])
        )
    for filename in ("reana.yaml", "reana.yml"):
        (directory / filename).unlink(missing_ok=True)
    if raw_yaml:
        (directory / "reana.yaml").write_text(yaml.safe_dump(raw))
    workflow.reana_specification = baseline
    workflow.launcher_url = "https://example.org/analysis/tree/main"
    session.commit()
    overrides = {"FROM": "fit", "TARGET": "fit", "CACHE": "off"}

    parameters = {
        "input_parameters": {"events": 2000},
        "operational_options": overrides,
    }
    if replacement:
        with app.test_client() as client:
            response = client.post(
                url_for(
                    "workflows.restart_workflow", workflow_id_or_name=str(workflow.id_)
                ),
                headers=auth_headers(user0),
                data={
                    "replacement": (
                        BytesIO(yaml.safe_dump(raw).encode()),
                        "reana.yaml",
                    ),
                    "parameters": json.dumps(parameters),
                },
            )
    else:
        response = post_restart(app, user0, auth_headers, workflow, **parameters)

    assert response.status_code == 200, response.json
    first, _, submission = publish.call_args.args
    assert submission["input_parameters"] == {"events": 2000}
    assert submission["operational_options"] == overrides
    assert first.launcher_url == workflow.launcher_url
    if not raw_yaml:
        assert first.reana_specification == baseline
        assert any(
            warning["code"] == "restart_stored_serial_specification"
            for warning in response.json["validation_warnings"]
        )
    # Emulate persisted run values from the controller, then restart that run.
    first.input_parameters = {"events": 2000}
    first.status = RunStatus.finished
    session.commit()
    if remove_yaml:
        (directory / "reana.yaml").unlink()
    response = post_restart(app, user0, auth_headers, first)

    assert response.status_code == 200, response.json
    second, _, submission = publish.call_args.args
    assert second.reana_specification["inputs"] == baseline["inputs"]
    assert second.operational_options == {"CACHE": "on"}
    assert submission["operational_options"] == {"CACHE": "on"}
    assert not submission.get("input_parameters")
    assert second.launcher_url == workflow.launcher_url
    assert workflow.reana_specification == baseline


def test_missing_specification_reports_original_launch_source(
    app, session, user0, auth_headers, restart_workspace
):
    """Offer launcher recovery guidance without claiming exact spec provenance."""
    workflow, directory, publish = restart_workspace
    for filename in ("reana.yaml", "reana.yml"):
        (directory / filename).unlink(missing_ok=True)
    workflow.type_ = "snakemake"
    workflow.launcher_url = "https://example.org/analysis/tree/main"
    session.commit()

    response = post_restart(app, user0, auth_headers, workflow)

    assert response.status_code == 400, response.json
    assert (
        "Original launch source: " + workflow.launcher_url in response.json["message"]
    )
    assert "may not identify the exact specification" in response.json["message"]
    publish.assert_not_called()


@pytest.mark.parametrize("change", ["type", "file"])
def test_changed_restart_reference_requires_replacement(
    app, session, user0, auth_headers, restart_workspace, change
):
    """An engine or main-file change requires an explicit replacement selection."""
    workflow, directory, publish = restart_workspace
    raw = write_sources(directory, "cwl")
    workflow.type_ = "serial" if change == "type" else "cwl"
    workflow.reana_specification = copy.deepcopy(raw)
    if change == "file":
        workflow.reana_specification["workflow"]["file"] = "previous.cwl"
    session.commit()
    count = session.query(Workflow).count()

    response = post_restart(app, user0, auth_headers, workflow)

    assert response.status_code == 400, response.json
    assert "workflow.type or workflow.file" in response.json["message"]
    assert "restart -f reana.yaml" in response.json["message"]
    assert session.query(Workflow).count() == count
    publish.assert_not_called()
    with app.test_client() as client:
        response = client.post(
            url_for(
                "workflows.restart_workflow", workflow_id_or_name=str(workflow.id_)
            ),
            headers=auth_headers(user0),
            data={"replacement": (BytesIO(yaml.safe_dump(raw).encode()), "reana.yaml")},
        )
    assert response.status_code == 200, response.json
    assert publish.call_args.args[0].type_ == "cwl"


@pytest.mark.parametrize(
    "output",
    ["symlink", "files", "scope_files", "directories", "bytes", "empty", "unreadable"],
)
def test_restart_legacy_directory_reports_post_run_output(
    app, session, user0, auth_headers, restart_workspace, monkeypatch, output
):
    """Post-run outputs in legacy scope produce actionable migration guidance."""
    workflow, directory, publish = restart_workspace
    raw = write_sources(directory, "snakemake")
    del raw["workflow"]["files"]
    raw["inputs"]["directories"] = ["data"]
    (directory / "data").mkdir()
    (directory / "data" / "input.txt").write_text("input")
    (directory / "reana.yaml").write_text(yaml.safe_dump(raw))
    workflow.type_ = "snakemake"
    workflow.reana_specification = copy.deepcopy(raw)
    session.commit()
    response = post_restart(app, user0, auth_headers, workflow)
    assert response.status_code == 200, response.json
    publish.reset_mock()
    if output == "symlink":
        (directory / "data" / "output-link").symlink_to("input.txt")
    elif output == "empty":
        (directory / "data" / "input.txt").unlink()
    elif output == "unreadable":
        original_scandir = os.scandir

        def unreadable_data(path):
            if not isinstance(path, int) and os.fspath(path).endswith("/data"):
                raise PermissionError("permission denied")
            return original_scandir(path)

        monkeypatch.setattr(
            "reana_commons.specification_paths.os.scandir", unreadable_data
        )
    elif output == "directories":
        monkeypatch.setattr(
            "reana_commons.specification_paths.SPECIFICATION_BUNDLE_MAX_DIRECTORIES", 1
        )
        (directory / "data" / "results").mkdir()
        (directory / "data" / "results" / "output.txt").write_text("output")
    else:
        if output == "files":
            monkeypatch.setattr(
                "reana_server.specification_bundles.REANA_SPEC_BUNDLE_MAX_FILES", 4
            )
        elif output == "scope_files":
            monkeypatch.setattr(
                "reana_commons.specification_paths.SPECIFICATION_BUNDLE_MAX_FILES", 4
            )
        else:
            initial_size = sum(
                (directory / filename).stat().st_size
                for filename in (
                    "reana.yaml",
                    "Snakefile",
                    "params.yaml",
                    "data/input.txt",
                )
            )
            monkeypatch.setattr(
                "reana_server.specification_bundles.REANA_SPEC_BUNDLE_MAX_BYTES",
                initial_size + 64,
            )
        (directory / "data" / "output.txt").write_text("x" * 200)
    count = session.query(Workflow).count()

    response = post_restart(app, user0, auth_headers, workflow)

    assert response.status_code == 400, response.json
    assert {
        "symlink": "symbolic link",
        "files": "too many files",
        "scope_files": "too many files",
        "directories": "too many directories",
        "bytes": "too large",
        "empty": "contains no regular files",
        "unreadable": "could not be read securely",
    }[output] in response.json["message"]
    assert response.json["message"].count("workflow.files/workflow.directories") == 1
    assert "include:" in response.json["message"]
    assert "configfile:" in response.json["message"]
    assert session.query(Workflow).count() == count
    publish.assert_not_called()


@pytest.mark.parametrize("restart", [False, True])
def test_unknown_parameters_remain_rejected(
    app, session, user0, auth_headers, restart_workspace, restart
):
    """Start and the legacy Serial restart both reject unknown run parameters."""
    workflow, directory, publish = restart_workspace
    for filename in ("reana.yaml", "reana.yml"):
        (directory / filename).unlink(missing_ok=True)
    workflow.status = RunStatus.finished if restart else RunStatus.created
    session.commit()
    count = session.query(Workflow).count()
    with app.test_client() as client:
        response = client.post(
            url_for("workflows.start_workflow", workflow_id_or_name=str(workflow.id_)),
            headers=auth_headers(user0),
            json={"restart": restart, "input_parameters": {"unknown": "value"}},
        )
    assert response.status_code == 400, response.json
    assert 'Input parameter "unknown"' in response.json["message"]
    assert session.query(Workflow).count() == count
    publish.assert_not_called()


def test_serial_restart_rejects_non_json_defaults_before_cloning(
    app, session, user0, auth_headers, restart_workspace
):
    """Run overrides cannot hide defaults that cannot be stored for later restarts."""
    from datetime import date

    workflow, directory, publish = restart_workspace
    raw = copy.deepcopy(workflow.reana_specification)
    raw.setdefault("inputs", {})["parameters"] = {"events": date(2018, 5, 1)}
    (directory / "reana.yaml").write_text(yaml.safe_dump(raw))
    count = session.query(Workflow).count()

    response = post_restart(
        app, user0, auth_headers, workflow, input_parameters={"events": 2000}
    )

    assert response.status_code == 400, response.json
    assert "JSON" in response.json["message"]
    assert session.query(Workflow).count() == count
    publish.assert_not_called()
