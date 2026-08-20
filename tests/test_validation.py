# This file is part of REANA.
# Copyright (C) 2022, 2025, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server tests for validation module."""

import io
import struct
import os
import pathlib
import shutil
import stat
import zipfile

import pytest
from unittest.mock import Mock, patch
from contextlib import nullcontext as does_not_raise

from flask import Flask
from werkzeug.datastructures import FileStorage, MultiDict
from werkzeug.exceptions import RequestEntityTooLarge

from reana_commons.config import (
    REANA_DEFAULT_SNAKEMAKE_ENV_IMAGE,
    REANA_WORKFLOW_UMASK,
)
from reana_commons.errors import REANASpecificationPathError, REANAValidationError

# The per-check server wrappers (``validate_inputs``/``validate_images``) have
# been removed: every server path now validates through the single shared
# validator (``validate_serialized_spec``). These low-level checks live in
# reana-commons, so the remaining unit tests target them there directly.
from reana_commons.validation.images import validate_images
from reana_commons.validation.utils import MAX_LOAD_ERROR_MESSAGE_CHARS, validate_inputs
from reana_server.rest import workflows
from reana_server import specification_bundles

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
    monkeypatch.setattr(workflows, "REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES", 10)
    app = Flask(__name__)
    with app.test_request_context(
        "/api/workflows/validate",
        method="POST",
        data=b"01234567890",
        content_type="multipart/form-data",
    ):
        with pytest.raises(RequestEntityTooLarge, match="too large"):
            workflows._validate_spec_bundle_request_size()


def test_bundle_request_sets_explicit_single_part_limit():
    """The framework parser and REANA contract enforce the same part count."""
    app = Flask(__name__)
    with app.test_request_context(
        "/api/workflows/validate",
        method="POST",
        data=b"",
        content_type="multipart/form-data",
    ):
        workflows._cap_bundle_request_body()
        assert workflows.request.max_form_parts == 1
        assert (
            workflows.request.max_content_length
            == workflows.REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES
        )


def test_multipart_upload_requires_seekable_temporary_stream():
    """A stream whose size cannot be established is rejected before forwarding."""

    class NonSeekableStream:
        def tell(self):
            raise OSError("not seekable")

    with pytest.raises(REANAValidationError, match="determine the uploaded file size"):
        workflows._remaining_stream_size(NonSeekableStream())


def test_bundle_request_cap_allows_bounded_zip_and_multipart_framing():
    """The HTTP cap has room beyond the extracted-content limit."""
    from reana_server import config

    assert (
        config.REANA_SPEC_BUNDLE_MAX_REQUEST_BYTES > config.REANA_SPEC_BUNDLE_MAX_BYTES
    )


def test_environment_check_is_offline():
    """Server environment checks only report offline tag findings."""
    specification = {
        "workflow": {
            "type": "serial",
            "specification": {
                "steps": [
                    {
                        "name": "step",
                        "environment": "docker.io/library/busybox:1",
                        "commands": ["true"],
                    }
                ]
            },
        }
    }
    with patch(
        "reana_server.validation.check_environment_tags", return_value=[]
    ) as check_mock:
        server_validation.check_spec_environment_tags(specification)

    check_mock.assert_called_once_with(["docker.io/library/busybox:1"])


def _uploaded_zip(entries, compression=zipfile.ZIP_STORED):
    """Build one uploaded ZIP bundle."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    stream.seek(0)
    return FileStorage(stream=stream, filename="validation-bundle.zip")


def _declared_entry_count_zip(entry_count):
    """Build a minimal EOCD record declaring ``entry_count`` members."""
    return io.BytesIO(
        struct.pack(
            "<4s4H2LH",
            b"PK\x05\x06",
            0,
            0,
            entry_count,
            entry_count,
            0,
            0,
            0,
        )
    )


def test_bundle_rejects_entry_count_before_zipfile(monkeypatch, tmp_path):
    """Excessive central-directory declarations never reach ``ZipFile``."""
    monkeypatch.setattr(specification_bundles, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(specification_bundles, "REANA_SPEC_BUNDLE_MAX_FILES", 1)
    zip_file = Mock(side_effect=AssertionError("ZipFile must not be constructed"))
    monkeypatch.setattr(specification_bundles.zipfile, "ZipFile", zip_file)
    storage = FileStorage(
        stream=_declared_entry_count_zip(2), filename="validation-bundle.zip"
    )

    with pytest.raises(REANAValidationError, match="too many files"):
        specification_bundles.extract_uploaded_bundle(storage, str(tmp_path))

    zip_file.assert_not_called()


def test_uploaded_bundle_regathers_legacy_external_workflow_scope(tmp_path):
    """Server scope equality accepts a complete legacy client snapshot."""
    specification = (
        "inputs:\n"
        "  directories: [workflow]\n"
        "workflow:\n"
        "  type: cwl\n"
        "  file: workflow/main.cwl\n"
    )
    storage = _uploaded_zip(
        [
            ("reana.yaml", specification),
            ("workflow/main.cwl", "class: Workflow"),
            ("workflow/step.cwl", "class: CommandLineTool"),
        ]
    )

    bundle = specification_bundles.extract_uploaded_bundle(storage, str(tmp_path))

    assert set(bundle.members) == {
        "reana.yaml",
        "workflow/main.cwl",
        "workflow/step.cwl",
    }
    assert os.path.isfile(os.path.join(bundle.absolute_path, "workflow", "step.cwl"))


def test_unloadable_uploaded_bundle_returns_canonical_member(tmp_path):
    """An unloadable canonical-only bundle still has a safe member mapping."""
    storage = _uploaded_zip([("reana.yaml", "workflow: [")])

    bundle = specification_bundles.extract_uploaded_bundle(storage, str(tmp_path))

    assert bundle.members == {
        "reana.yaml": os.path.join(bundle.absolute_path, "reana.yaml")
    }


def test_zip64_archives_are_rejected_before_zipfile():
    """ZIP64 is unnecessary under REANA's limits and is rejected outright."""
    zip64_record = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        2,
        2,
        0,
        0,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)
    eocd = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )

    with pytest.raises(ValueError, match="ZIP64 archives are not supported"):
        specification_bundles.preflight_zip_metadata(
            io.BytesIO(zip64_record + locator + eocd), 1
        )


def test_zip64_locator_is_rejected_without_classic_sentinels():
    """A ZIP64 locator is authoritative even if classic EOCD values look small."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("reana.yaml", "workflow: {type: serial}")
    contents = stream.getvalue()
    eocd_offset = contents.rfind(b"PK\x05\x06")
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)

    with pytest.raises(ValueError, match="ZIP64 archives are not supported"):
        specification_bundles.preflight_zip_metadata(
            io.BytesIO(contents[:eocd_offset] + locator + contents[eocd_offset:]), 2
        )


def test_zip_preflight_rejects_underreported_entry_count():
    """The scanner enforces the actual central-directory count, not only EOCD."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("reana.yaml", "workflow: {type: serial}")
        archive.writestr("Snakefile", "rule all: input: []")
    contents = bytearray(stream.getvalue())
    eocd_offset = contents.rfind(b"PK\x05\x06")
    # entries_on_disk and total_entries are adjacent 16-bit fields.
    struct.pack_into("<HH", contents, eocd_offset + 8, 1, 1)

    with pytest.raises(ValueError, match="entry count does not match"):
        specification_bundles.preflight_zip_metadata(io.BytesIO(contents), 2)


def test_zip_preflight_ignores_eocd_signature_inside_comment():
    """An EOCD-like byte sequence in a valid ZIP comment is not selected."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("reana.yaml", "workflow: {type: serial}")
        archive.comment = b"comment with PK\x05\x06 marker and trailing bytes"
    stream.seek(0)

    specification_bundles.preflight_zip_metadata(stream, 1)


def test_stage_validation_bundle_rejects_too_many_files(monkeypatch, tmp_path):
    """Bundle staging is bounded by the configured file-count limit."""
    monkeypatch.setattr(specification_bundles, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(specification_bundles, "REANA_SPEC_BUNDLE_MAX_FILES", 1)

    with pytest.raises(REANAValidationError, match="too many files"):
        workflows._stage_validation_bundle(
            {
                "bundle": _uploaded_zip(
                    [
                        (
                            "reana.yaml",
                            b"workflow:\n  type: snakemake\n  file: Snakefile\n",
                        ),
                        ("Snakefile", b""),
                    ]
                )
            }
        )


def test_stage_validation_bundle_rejects_overlong_member_path(monkeypatch, tmp_path):
    """ZIP metadata is bounded independently of extracted content bytes."""
    monkeypatch.setattr(specification_bundles, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(specification_bundles, "REANA_SPEC_BUNDLE_MAX_PATH_BYTES", 10)

    with pytest.raises(REANAValidationError, match="encoded bytes"):
        workflows._stage_validation_bundle(
            {
                "bundle": _uploaded_zip(
                    [
                        (
                            "reana.yaml",
                            b"workflow:\n  type: serial\n"
                            b"  files: [long-name.yaml]\n"
                            b"  specification: {steps: []}\n",
                        ),
                        ("long-name.yaml", b"value: 1\n"),
                    ]
                )
            }
        )


def test_stage_validation_bundle_rejects_excessive_depth(monkeypatch, tmp_path):
    """Direct ZIP callers cannot bypass the fixed component-depth contract."""
    monkeypatch.setattr(specification_bundles, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(specification_bundles, "SPECIFICATION_BUNDLE_MAX_DEPTH", 2)

    with pytest.raises(REANAValidationError, match="maximum depth"):
        workflows._stage_validation_bundle(
            {
                "bundle": _uploaded_zip(
                    [
                        (
                            "reana.yaml",
                            b"workflow:\n  type: serial\n"
                            b"  files: [one/two/value]\n"
                            b"  specification: {steps: []}\n",
                        ),
                        ("one/two/value", b"value"),
                    ]
                )
            }
        )


def test_stage_validation_bundle_rejects_too_many_implicit_directories(
    monkeypatch, tmp_path
):
    """ZIP paths have a cumulative implicit-parent directory budget."""
    monkeypatch.setattr(specification_bundles, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(
        specification_bundles, "SPECIFICATION_BUNDLE_MAX_DIRECTORIES", 1
    )

    with pytest.raises(REANAValidationError, match="too many directories"):
        workflows._stage_validation_bundle(
            {
                "bundle": _uploaded_zip(
                    [
                        (
                            "reana.yaml",
                            b"workflow:\n  type: serial\n"
                            b"  files: [one/value, two/value]\n"
                            b"  specification: {steps: []}\n",
                        ),
                        ("one/value", b"one"),
                        ("two/value", b"two"),
                    ]
                )
            }
        )


def test_stage_validation_bundle_rejects_duplicate_bundle_fields(monkeypatch, tmp_path):
    """Duplicate same-name multipart fields cannot bypass the one-part contract."""
    monkeypatch.setattr(specification_bundles, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    files = MultiDict(
        [
            (
                "bundle",
                _uploaded_zip(
                    [
                        (
                            "reana.yaml",
                            b"workflow:\n  type: serial\n"
                            b"  specification: {steps: []}\n",
                        )
                    ]
                ),
            ),
            ("bundle", _uploaded_zip([("reana.yaml", b"other: value\n")])),
        ]
    )
    with pytest.raises(REANAValidationError, match="exactly one"):
        workflows._stage_validation_bundle(files)


@pytest.mark.parametrize(
    "colliding_entries",
    [
        [("a", b"file"), ("a/b", b"nested")],
        [("a/b", b"nested"), ("a", b"file")],
    ],
)
def test_stage_validation_bundle_rejects_file_ancestor_collision(
    monkeypatch, tmp_path, colliding_entries
):
    """A regular ZIP member cannot also be an ancestor of another member."""
    monkeypatch.setattr(specification_bundles, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    entries = [
        (
            "reana.yaml",
            b"workflow:\n  type: serial\n  specification: {steps: []}\n",
        ),
        *colliding_entries,
    ]

    with pytest.raises(REANAValidationError, match="nested below a regular file"):
        workflows._stage_validation_bundle({"bundle": _uploaded_zip(entries)})

    assert not list(
        (tmp_path / specification_bundles.VALIDATION_STAGING_SUBDIR).glob("*")
    )


def test_stage_validation_bundle_rejects_unsafe_member_and_cleans_up(
    monkeypatch, tmp_path
):
    """Unsafe relative paths are rejected and partial staging is removed."""
    monkeypatch.setattr(specification_bundles, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))

    with pytest.raises(REANAValidationError, match="Unsafe bundle path"):
        workflows._stage_validation_bundle(
            {
                "bundle": _uploaded_zip(
                    [
                        (
                            "reana.yaml",
                            b"workflow:\n  type: serial\n  specification: {steps: []}\n",
                        ),
                        ("../escape", b""),
                    ]
                )
            }
        )

    assert not list(
        (tmp_path / specification_bundles.VALIDATION_STAGING_SUBDIR).glob("*")
    )


def test_stage_validation_bundle_enforces_size_cap_while_streaming(
    monkeypatch, tmp_path
):
    """An oversized member is rejected mid-stream, even with no Content-Length.

    The cap is enforced while streaming each member to disk, so a chunked upload
    (which bypasses the up-front Content-Length check) cannot land a huge file.
    """
    monkeypatch.setattr(specification_bundles, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(specification_bundles, "REANA_SPEC_BUNDLE_MAX_BYTES", 8)

    with pytest.raises(REANAValidationError, match="too large"):
        workflows._stage_validation_bundle(
            {
                "bundle": _uploaded_zip(
                    [
                        (
                            "reana.yaml",
                            b"workflow:\n  type: serial\n  specification: {steps: []}\n",
                        )
                    ]
                )
            }
        )

    assert not list(
        (tmp_path / specification_bundles.VALIDATION_STAGING_SUBDIR).glob("*")
    )


def test_stage_validation_bundle_rejects_compressed_and_link_entries(
    monkeypatch, tmp_path
):
    """Validation ZIPs are uncompressed and regular-file-only."""
    monkeypatch.setattr(specification_bundles, "SHARED_VOLUME_PATH", str(tmp_path))
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    specification = b"workflow:\n  type: serial\n  specification: {steps: []}\n"
    with pytest.raises(REANAValidationError, match="uncompressed"):
        workflows._stage_validation_bundle(
            {
                "bundle": _uploaded_zip(
                    [("reana.yaml", specification)],
                    compression=zipfile.ZIP_DEFLATED,
                )
            }
        )

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("reana.yaml", specification)
        link = zipfile.ZipInfo("linked")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "target")
    stream.seek(0)
    with pytest.raises(REANAValidationError, match="regular files"):
        workflows._stage_validation_bundle(
            {"bundle": FileStorage(stream=stream, filename="bundle.zip")}
        )


def test_stage_validation_bundle_is_readable_by_validator_group(monkeypatch, tmp_path):
    """Snapshots and nested files preserve the configured shared-volume group."""
    monkeypatch.setattr(workflows, "SHARED_VOLUME_PATH", str(tmp_path))
    absolute_path = None
    try:
        bundle = workflows._stage_validation_bundle(
            {
                "bundle": _uploaded_zip(
                    [
                        (
                            "reana.yaml",
                            b"workflow:\n"
                            b"  type: serial\n"
                            b"  files: [rules/common.yaml]\n"
                            b"  specification: {steps: []}\n",
                        ),
                        ("rules/common.yaml", b"common: true\n"),
                    ]
                )
            }
        )
        absolute_path = bundle.absolute_path
        shared_gid = os.stat(tmp_path).st_gid
        assert stat.S_IMODE(os.stat(absolute_path).st_mode) == 0o2750
        assert (
            stat.S_IMODE(os.stat(os.path.join(absolute_path, "reana.yaml")).st_mode)
            == 0o640
        )
        for path in (
            absolute_path,
            os.path.join(absolute_path, "reana.yaml"),
            os.path.join(absolute_path, "rules"),
            os.path.join(absolute_path, "rules", "common.yaml"),
        ):
            assert os.stat(path).st_gid == shared_gid
    finally:
        if absolute_path:
            shutil.rmtree(absolute_path, ignore_errors=True)


def test_seed_workspace_uses_shared_permissions(tmp_path):
    """Seeded sources are accessible to runtime containers in the shared group."""
    source = tmp_path / "source"
    (source / "workflow").mkdir(parents=True)
    files = {
        "reana.yaml": "workflow:\n  type: snakemake\n  file: workflow/Snakefile\n",
        "workflow/Snakefile": "rule all:\n  input: 'result.txt'\n",
    }
    members = {}
    for relative_path, contents in files.items():
        path = source / relative_path
        path.write_text(contents)
        members[relative_path] = str(path)

    workspace = tmp_path / "workspace"
    previous_umask = os.umask(REANA_WORKFLOW_UMASK)
    try:
        copied = specification_bundles.seed_workspace(members, str(workspace))
    finally:
        os.umask(previous_umask)

    expected_directory_mode = 0o777 & ~REANA_WORKFLOW_UMASK
    expected_file_mode = 0o666 & ~REANA_WORKFLOW_UMASK
    assert copied == sum(len(contents) for contents in files.values())
    assert stat.S_IMODE(workspace.stat().st_mode) == expected_directory_mode
    assert (
        stat.S_IMODE((workspace / "workflow").stat().st_mode) == expected_directory_mode
    )
    assert stat.S_IMODE((workspace / "reana.yaml").stat().st_mode) == expected_file_mode
    assert stat.S_IMODE((workspace / "workflow" / "Snakefile").stat().st_mode) == (
        expected_file_mode
    )
    assert (workspace / "workflow" / "Snakefile").read_text() == files[
        "workflow/Snakefile"
    ]


def test_stage_validation_snapshot_rejects_ancestor_swap(monkeypatch, tmp_path):
    """A selected source cannot be redirected through a swapped ancestor."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "reana.yaml").write_text(
        "workflow:\n  type: snakemake\n  file: defs/Snakefile\n"
    )
    (workspace / "defs").mkdir()
    (workspace / "defs" / "Snakefile").write_text("ORIGINAL")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Snakefile").write_text("OUTSIDE")
    shared = tmp_path / "shared"
    shared.mkdir()

    gather = specification_bundles.gather_validation_members

    def gather_then_swap(path, source_base_directory=None, **kwargs):
        result = gather(path, source_base_directory=source_base_directory, **kwargs)
        (workspace / "defs").rename(workspace / "defs-original")
        (workspace / "defs").symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(
        specification_bundles,
        "gather_validation_members",
        gather_then_swap,
    )
    with pytest.raises(REANAValidationError, match="securely open"):
        specification_bundles.stage_validation_snapshot(
            str(workspace / "reana.yaml"), str(shared)
        )
    staging = shared / specification_bundles.VALIDATION_STAGING_SUBDIR
    assert not staging.exists() or not list(staging.iterdir())


def test_stage_validation_snapshot_bounds_yaml_before_scope_discovery(
    monkeypatch, tmp_path
):
    """Canonical YAML is size-limited before scope discovery can parse it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "reana.yaml").write_text("workflow: {type: serial}\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(specification_bundles, "REANA_SPEC_BUNDLE_MAX_BYTES", 8)

    def unexpected_gather(*args, **kwargs):
        pytest.fail("oversized YAML reached scope discovery")

    monkeypatch.setattr(
        specification_bundles,
        "gather_validation_members",
        unexpected_gather,
    )
    with pytest.raises(REANAValidationError, match="too large"):
        specification_bundles.stage_validation_snapshot(
            str(workspace / "reana.yaml"), str(shared)
        )

    staging = shared / specification_bundles.VALIDATION_STAGING_SUBDIR
    assert not staging.exists() or not list(staging.iterdir())


def test_stage_validation_snapshot_uses_copied_yaml_for_scope(monkeypatch, tmp_path):
    """Workspace YAML changes after copying cannot change snapshot membership."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = "workflow:\n  type: snakemake\n  file: Snakefile\n"
    (workspace / "reana.yaml").write_text(original)
    (workspace / "Snakefile").write_text("ORIGINAL")
    shared = tmp_path / "shared"
    shared.mkdir()
    gather = specification_bundles.gather_validation_members

    def mutate_then_gather(path, source_base_directory=None, **kwargs):
        (workspace / "reana.yaml").write_text(
            "workflow:\n  type: snakemake\n  file: Otherfile\n"
        )
        return gather(path, source_base_directory=source_base_directory, **kwargs)

    monkeypatch.setattr(
        specification_bundles,
        "gather_validation_members",
        mutate_then_gather,
    )
    staged = None
    try:
        staged, _relative, _bytes, _legacy = (
            specification_bundles.stage_validation_snapshot(
                str(workspace / "reana.yaml"), str(shared)
            )
        )
        assert pathlib.Path(staged, "reana.yaml").read_text() == original
        assert pathlib.Path(staged, "Snakefile").read_text() == "ORIGINAL"
        assert not pathlib.Path(staged, "Otherfile").exists()
    finally:
        if staged:
            shutil.rmtree(staged)


def test_stage_validation_snapshot_canonicalizes_selected_filename(tmp_path):
    """Descriptor-relative copying keeps the canonical archive destination name."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    specification = workspace / "selected.yaml"
    contents = "workflow:\n  type: serial\n  specification: {steps: []}\n"
    specification.write_text(contents)
    shared = tmp_path / "shared"
    shared.mkdir()

    absolute_path = None
    try:
        absolute_path, _relative, _bytes, _legacy = (
            specification_bundles.stage_validation_snapshot(
                str(specification), str(shared)
            )
        )
        assert pathlib.Path(absolute_path, "reana.yaml").read_text() == contents
    finally:
        if absolute_path:
            shutil.rmtree(absolute_path, ignore_errors=True)


def test_stage_validation_snapshot_deduplicates_legacy_canonical_input(tmp_path):
    """A copied reana.yaml remains identical to its legacy input declaration."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    specification = workspace / "reana.yaml"
    specification.write_text(
        "inputs:\n"
        "  files: [reana.yaml]\n"
        "workflow:\n"
        "  type: cwl\n"
        "  file: main.cwl\n"
    )
    (workspace / "main.cwl").write_text("class: Workflow")
    shared = tmp_path / "shared"
    shared.mkdir()

    absolute_path = None
    try:
        absolute_path, _relative, _bytes, _legacy = (
            specification_bundles.stage_validation_snapshot(
                str(specification), str(shared)
            )
        )
        assert sorted(path.name for path in pathlib.Path(absolute_path).iterdir()) == [
            "main.cwl",
            "reana.yaml",
        ]
    finally:
        if absolute_path:
            shutil.rmtree(absolute_path, ignore_errors=True)


def test_stage_validation_snapshot_rejects_sibling_canonical_input(tmp_path):
    """A copied differently named selection cannot alias a sibling reana.yaml."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "reana.yaml").write_text("workflow: {type: serial}\n")
    specification = workspace / "selected.yaml"
    specification.write_text(
        "inputs:\n"
        "  files: [reana.yaml]\n"
        "workflow:\n"
        "  type: cwl\n"
        "  file: main.cwl\n"
    )
    (workspace / "main.cwl").write_text("class: Workflow")
    shared = tmp_path / "shared"
    shared.mkdir()

    with pytest.raises(REANASpecificationPathError) as error:
        specification_bundles.stage_validation_snapshot(str(specification), str(shared))

    assert error.value.reason == "conflict"
    assert error.value.field == "inputs.files"


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


@pytest.mark.parametrize("exit_code", [None, False, True, -1, 3, 255])
def test_authoritative_report_unknown_exit_code_is_service_error(exit_code):
    """Any undocumented validator exit code fails closed."""
    report = {
        "reana_specification": _candidate_serial_spec("docker.io/library/busybox:1.36"),
        "error": None,
    }
    with pytest.raises(SpecValidationServiceError, match="Unexpected.*exit code"):
        _authoritative_report(report, exit_code=exit_code, policy={})


def test_authoritative_report_success_without_candidate_is_service_error():
    """Exit code 0 is valid only when it carries a loaded candidate."""
    report = {"reana_specification": None, "error": None}
    with pytest.raises(SpecValidationServiceError, match="without a candidate"):
        _authoritative_report(report, exit_code=0, policy={})


def test_authoritative_report_load_failure_with_candidate_is_service_error():
    """Exit code 1 cannot carry a contradictory successfully loaded candidate."""
    report = {
        "reana_specification": _candidate_serial_spec("docker.io/library/busybox:1.36"),
        "error": {"code": "load", "message": "bad Snakefile"},
    }
    with pytest.raises(SpecValidationServiceError, match="candidate.*failed load"):
        _authoritative_report(report, exit_code=1, policy={})


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
    (tmp_path / "reana.yaml").write_text("version: 0.3.0\nworkflow:\n  type: serial\n")
    monkeypatch.setattr(server_validation, "build_validation_policy", lambda: {})

    def _boom(*args, **kwargs):
        raise RuntimeError(
            "[Errno 2] No such file or directory: 'code/helloworld.py'\nframe"
        )

    monkeypatch.setattr("reana_commons.specification.load_reana_spec", _boom)

    report = server_validation.validate_spec_bundle(str(tmp_path), "validation-tmp/x")
    assert report["valid"] is False
    assert report["reana_specification"] is None
    assert report["errors"][0]["code"] == "load"
    assert report["errors"][0]["message"] == (
        "[Errno 2] No such file or directory: 'code/helloworld.py'"
    )


def test_validate_spec_bundle_warns_about_legacy_external_workflow_scope(
    monkeypatch, tmp_path
):
    """Legacy external workflow inputs produce an actionable warning."""
    (tmp_path / "workflow.cwl").write_text("class: Workflow")
    (tmp_path / "step.cwl").write_text("class: CommandLineTool")
    (tmp_path / "reana.yaml").write_text(
        "inputs:\n"
        "  files: [step.cwl]\n"
        "workflow:\n"
        "  type: cwl\n"
        "  file: workflow.cwl\n"
    )
    monkeypatch.setattr(server_validation, "build_validation_policy", lambda: {})
    monkeypatch.setattr(
        server_validation,
        "_call_rwc_validate",
        lambda _path: (
            0,
            {
                "reana_specification": _candidate_serial_spec(
                    "docker.io/library/busybox:1.36"
                ),
                "error": None,
            },
        ),
    )

    report = server_validation.validate_spec_bundle(str(tmp_path), "validation-tmp/x")

    assert report["warnings"][-1]["code"] == ("deprecated_input_workflow_sources")


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


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="list-payload"),
        pytest.param({}, id="missing-report"),
        pytest.param({"report": None}, id="null-report"),
        pytest.param({"report": []}, id="list-report"),
    ],
)
def test_call_rwc_validate_malformed_success_is_service_error(monkeypatch, payload):
    """A malformed successful controller response is an infrastructure failure."""

    class _Resp:
        ok = True

        def json(self):
            return payload

    monkeypatch.setattr(server_validation.requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(SpecValidationServiceError, match="malformed response"):
        _call_rwc_validate("validation-tmp/x")


def test_call_rwc_validate_non_json_success_is_service_error(monkeypatch):
    """A non-JSON successful controller response is an infrastructure failure."""

    class _Resp:
        ok = True

        def json(self):
            raise ValueError("not JSON")

    monkeypatch.setattr(server_validation.requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(SpecValidationServiceError, match="malformed response"):
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
