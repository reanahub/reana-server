# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Secure staging helpers for workflow specification snapshots."""

import os
import posixpath
import shutil
import stat
import struct
import uuid
import zipfile
from typing import Dict, NamedTuple, Tuple

from reana_commons.errors import (
    REANASpecificationScopeError,
    REANAValidationError,
)
from reana_commons.specification_paths import (
    CANONICAL_REANA_SPECIFICATION,
    SPECIFICATION_BUNDLE_MAX_DEPTH,
    SPECIFICATION_BUNDLE_MAX_DIRECTORIES,
    gather_validation_members,
    gather_workspace_seed_members,
    open_regular_file_beneath,
)

from reana_server.config import (
    REANA_SPEC_BUNDLE_MAX_BYTES,
    REANA_SPEC_BUNDLE_MAX_FILES,
    REANA_SPEC_BUNDLE_MAX_PATH_BYTES,
    SHARED_VOLUME_PATH,
)

VALIDATION_STAGING_SUBDIR = "validation-tmp"
_COPY_CHUNK_SIZE = 1024 * 1024
ZIP_MAXIMUM_CENTRAL_DIRECTORY_BYTES = 64 * 1024 * 1024
_ZIP_EOCD = struct.Struct("<4s4H2LH")
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP_CENTRAL_DIRECTORY_ENTRY = struct.Struct("<4s6H3L5H2L")


class ExtractedSpecificationBundle(NamedTuple):
    """Validated specification bundle staged on the shared volume."""

    absolute_path: str
    relative_path: str
    total_bytes: int
    legacy_parameters: bool
    members: Dict[str, str]


def preflight_zip_metadata(  # noqa: C901
    stream,
    maximum_entries: int,
    maximum_central_directory_bytes: int = ZIP_MAXIMUM_CENTRAL_DIRECTORY_BYTES,
) -> None:
    """Reject excessive ZIP metadata before ``ZipFile`` materialises entries."""
    original_position = stream.tell()
    try:
        stream.seek(0, os.SEEK_END)
        archive_size = stream.tell()
        tail_size = min(archive_size, _ZIP_EOCD.size + 65535)
        stream.seek(archive_size - tail_size)
        tail = stream.read(tail_size)
        eocd_index = -1
        search_end = len(tail)
        while search_end:
            candidate = tail.rfind(b"PK\x05\x06", 0, search_end)
            if candidate < 0:
                break
            if len(tail) - candidate >= _ZIP_EOCD.size:
                candidate_comment_size = _ZIP_EOCD.unpack_from(tail, candidate)[-1]
                if candidate + _ZIP_EOCD.size + candidate_comment_size == len(tail):
                    eocd_index = candidate
                    break
            search_end = candidate
        if eocd_index < 0:
            raise ValueError("ZIP end-of-central-directory record is missing")
        eocd_offset = archive_size - tail_size + eocd_index
        (
            _signature,
            disk_number,
            directory_disk,
            entries_on_disk,
            entry_count,
            directory_size,
            directory_offset,
            _comment_size,
        ) = _ZIP_EOCD.unpack_from(tail, eocd_index)
        if disk_number or directory_disk or entries_on_disk != entry_count:
            raise ValueError("multi-disk ZIP archives are not supported")

        locator_offset = eocd_offset - _ZIP64_LOCATOR.size
        if locator_offset >= 0:
            stream.seek(locator_offset)
            if stream.read(4) == b"PK\x06\x07":
                raise ValueError("ZIP64 archives are not supported")
        if (
            entry_count == 0xFFFF
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
        ):
            raise ValueError("ZIP64 archives are not supported")

        if entry_count > maximum_entries:
            raise ValueError(
                "ZIP archive contains too many entries (maximum is {})".format(
                    maximum_entries
                )
            )
        if directory_size > maximum_central_directory_bytes:
            raise ValueError(
                "ZIP central directory is too large (maximum is {} bytes)".format(
                    maximum_central_directory_bytes
                )
            )
        if directory_offset + directory_size != eocd_offset:
            raise ValueError(
                "ZIP central directory offset or size does not match the archive"
            )

        stream.seek(directory_offset)
        consumed = 0
        actual_entries = 0
        while consumed < directory_size:
            header = stream.read(_ZIP_CENTRAL_DIRECTORY_ENTRY.size)
            if len(header) != _ZIP_CENTRAL_DIRECTORY_ENTRY.size:
                raise ValueError("ZIP central directory entry is truncated")
            (
                signature,
                _created_by,
                _extract_version,
                _flags,
                _compression,
                _modified_time,
                _modified_date,
                _crc,
                compressed_size,
                uncompressed_size,
                filename_size,
                extra_size,
                comment_size,
                entry_disk,
                _internal_attributes,
                _external_attributes,
                local_header_offset,
            ) = _ZIP_CENTRAL_DIRECTORY_ENTRY.unpack(header)
            if signature != b"PK\x01\x02":
                raise ValueError("ZIP central directory entry is malformed")
            if entry_disk:
                raise ValueError("multi-disk ZIP archives are not supported")
            if (
                compressed_size == 0xFFFFFFFF
                or uncompressed_size == 0xFFFFFFFF
                or local_header_offset == 0xFFFFFFFF
            ):
                raise ValueError("ZIP64 archives are not supported")
            variable_size = filename_size + extra_size + comment_size
            consumed += _ZIP_CENTRAL_DIRECTORY_ENTRY.size + variable_size
            if consumed > directory_size:
                raise ValueError(
                    "ZIP central directory entry exceeds its declared size"
                )
            stream.seek(variable_size, os.SEEK_CUR)
            actual_entries += 1
            if actual_entries > maximum_entries:
                raise ValueError(
                    "ZIP archive contains too many entries (maximum is {})".format(
                        maximum_entries
                    )
                )
        if consumed != directory_size or actual_entries != entry_count:
            raise ValueError(
                "ZIP central directory entry count does not match metadata"
            )
    finally:
        stream.seek(original_position)


def _unsafe_archive_path(name: str) -> bool:
    """Return whether an archive member name violates the POSIX path contract."""
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or (len(name) >= 2 and name[1] == ":")
    ):
        return True
    normalized = posixpath.normpath(name)
    return (
        normalized in ("", ".", "..")
        or normalized != name
        or any(part in ("", ".", "..") for part in normalized.split("/"))
    )


def _regular_zip_member(info: zipfile.ZipInfo) -> bool:
    """Whether ``info`` represents a regular file (or has no Unix type bits)."""
    if info.is_dir():
        return False
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    return file_type in (0, stat.S_IFREG)


def _write_stream(destination: str, source, already_written: int) -> int:
    """Write ``source`` exclusively without following links and enforce limits."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o640)
    os.fchmod(descriptor, 0o640)
    total = already_written
    try:
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > REANA_SPEC_BUNDLE_MAX_BYTES:
                    raise REANAValidationError(
                        "Specification bundle is too large (maximum is {} bytes).".format(
                            REANA_SPEC_BUNDLE_MAX_BYTES
                        )
                    )
                output.write(chunk)
    except Exception:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise
    return total


def _fresh_staging_directory(
    shared_volume_path: str = None,
) -> Tuple[str, str]:
    shared_volume_path = shared_volume_path or SHARED_VOLUME_PATH
    staging_root = os.path.join(shared_volume_path, VALIDATION_STAGING_SUBDIR)
    try:
        os.mkdir(staging_root, mode=0o2750)
    except FileExistsError:
        mode = os.lstat(staging_root).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise REANAValidationError(
                "The validation staging root is not a regular directory."
            )
    # Preserve the shared-volume group on every descendant. This supports a
    # non-default fsGroup without exposing snapshots to unrelated users.
    os.chmod(staging_root, 0o2750)

    identifier = uuid.uuid4().hex
    relative_path = posixpath.join(VALIDATION_STAGING_SUBDIR, identifier)
    absolute_path = os.path.join(staging_root, identifier)
    os.mkdir(absolute_path, mode=0o2750)
    os.chmod(absolute_path, 0o2750)
    return absolute_path, relative_path


def _validate_archive_entries(entries):
    """Validate uploaded ZIP metadata and return unique member names."""
    if not entries:
        raise REANAValidationError("The specification bundle is empty.")
    if len(entries) > REANA_SPEC_BUNDLE_MAX_FILES:
        raise REANAValidationError(
            "Specification bundle has too many files (maximum is {}).".format(
                REANA_SPEC_BUNDLE_MAX_FILES
            )
        )

    names = set()
    directories = set()
    declared_size = 0
    for info in entries:
        if _unsafe_archive_path(info.filename):
            raise REANAValidationError("Unsafe bundle path: {}".format(info.filename))
        if len(info.filename.encode("utf-8")) > REANA_SPEC_BUNDLE_MAX_PATH_BYTES:
            raise REANAValidationError(
                "Bundle path exceeds {} encoded bytes: {}".format(
                    REANA_SPEC_BUNDLE_MAX_PATH_BYTES, info.filename
                )
            )
        components = info.filename.split("/")
        if len(components) > SPECIFICATION_BUNDLE_MAX_DEPTH:
            raise REANAValidationError(
                "Bundle path exceeds the maximum depth of {} components: {}".format(
                    SPECIFICATION_BUNDLE_MAX_DEPTH, info.filename
                )
            )
        for index in range(1, len(components)):
            directories.add("/".join(components[:index]))
            if len(directories) > SPECIFICATION_BUNDLE_MAX_DIRECTORIES:
                raise REANAValidationError(
                    "Specification bundle has too many directories "
                    "(maximum is {}).".format(SPECIFICATION_BUNDLE_MAX_DIRECTORIES)
                )
        if info.filename in names:
            raise REANAValidationError(
                "Duplicate bundle path: {}".format(info.filename)
            )
        names.add(info.filename)
        if info.flag_bits & 0x1:
            raise REANAValidationError("Encrypted bundle entries are not supported.")
        if info.compress_type != zipfile.ZIP_STORED:
            raise REANAValidationError(
                "Specification bundles must use uncompressed ZIP entries."
            )
        if not _regular_zip_member(info):
            raise REANAValidationError(
                "Only regular files are allowed in specification bundles: "
                "{}".format(info.filename)
            )
        declared_size += info.file_size
        if declared_size > REANA_SPEC_BUNDLE_MAX_BYTES:
            raise REANAValidationError(
                "Specification bundle is too large (maximum is {} bytes).".format(
                    REANA_SPEC_BUNDLE_MAX_BYTES
                )
            )
    for name in names:
        components = name.split("/")
        for index in range(1, len(components)):
            parent = "/".join(components[:index])
            if parent in names:
                raise REANAValidationError(
                    "Bundle path is nested below a regular file: {}".format(parent)
                )
    return names


def _extract_archive_entries(archive, entries, absolute_path):
    """Extract validated ZIP entries and return their cumulative bytes."""
    total_bytes = 0
    for info in entries:
        destination = os.path.join(absolute_path, *info.filename.split("/"))
        destination_directory = os.path.dirname(destination)
        try:
            os.makedirs(destination_directory, mode=0o2750, exist_ok=True)
            os.chmod(destination_directory, 0o2750)
            with archive.open(info, "r") as source:
                before = total_bytes
                total_bytes = _write_stream(destination, source, total_bytes)
            if total_bytes - before != info.file_size:
                raise REANAValidationError(
                    "Bundle entry size changed while extracting: {}".format(
                        info.filename
                    )
                )
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
            raise REANAValidationError(
                "Could not extract bundle entry {}: {}".format(info.filename, exc)
            )
    return total_bytes


def extract_uploaded_bundle(
    storage, shared_volume_path: str = None
) -> ExtractedSpecificationBundle:
    """Extract and validate one uploaded ``ZIP_STORED`` specification snapshot.

    The archive must contain exactly the members selected by its own canonical
    ``reana.yaml``. This makes the server, rather than the client, authoritative
    for the validation scope.
    """
    absolute_path, relative_path = _fresh_staging_directory(shared_volume_path)
    try:
        storage.stream.seek(0)
        try:
            preflight_zip_metadata(storage.stream, REANA_SPEC_BUNDLE_MAX_FILES)
            archive = zipfile.ZipFile(storage.stream, mode="r")
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            message = str(exc).replace(
                "ZIP archive contains too many entries",
                "Specification bundle has too many files",
            )
            raise REANAValidationError(
                "The specification bundle is not a valid ZIP archive: {}".format(
                    message
                )
            )

        with archive:
            entries = archive.infolist()
            names = _validate_archive_entries(entries)
            total_bytes = _extract_archive_entries(archive, entries, absolute_path)

        specification_path = os.path.join(absolute_path, CANONICAL_REANA_SPECIFICATION)
        if not os.path.isfile(specification_path):
            raise REANAValidationError(
                "The specification bundle must contain a top-level reana.yaml."
            )
        try:
            selected, _specification, legacy_parameters = gather_validation_members(
                specification_path
            )
        except REANASpecificationScopeError:
            # Let the validation endpoint turn an unloadable specification into
            # its structured ``load`` report.  There is no trustworthy declared
            # scope in this case, so only the canonical file itself is allowed.
            if names == {CANONICAL_REANA_SPECIFICATION}:
                return ExtractedSpecificationBundle(
                    absolute_path,
                    relative_path,
                    total_bytes,
                    False,
                    {CANONICAL_REANA_SPECIFICATION: specification_path},
                )
            raise
        selected_names = set(selected)
        if names != selected_names:
            unexpected = sorted(names - selected_names)
            missing = sorted(selected_names - names)
            details = []
            if unexpected:
                details.append("undeclared: {}".format(", ".join(unexpected)))
            if missing:
                details.append("missing: {}".format(", ".join(missing)))
            raise REANAValidationError(
                "Specification bundle does not match its declared validation "
                "scope ({})".format("; ".join(details))
            )
        return ExtractedSpecificationBundle(
            absolute_path,
            relative_path,
            total_bytes,
            legacy_parameters,
            selected,
        )
    except Exception:
        shutil.rmtree(absolute_path, ignore_errors=True)
        raise


def stage_validation_snapshot(
    specification_path: str,
    shared_volume_path: str = None,
    source_base_directory: str = None,
) -> Tuple[str, str, int, bool]:
    """Copy the declared validation scope into a fresh shared staging directory."""
    absolute_path, relative_path = _fresh_staging_directory(shared_volume_path)
    total_bytes = 0
    base_directory = source_base_directory or os.path.dirname(
        os.path.abspath(specification_path)
    )
    base_directory = os.path.abspath(base_directory)
    specification_directory = os.path.dirname(os.path.abspath(specification_path))
    try:
        staged_specification = os.path.join(
            absolute_path, CANONICAL_REANA_SPECIFICATION
        )
        descriptor = open_regular_file_beneath(
            specification_directory,
            os.path.basename(os.path.abspath(specification_path)),
            "REANA specification",
        )
        with os.fdopen(descriptor, "rb") as source:
            total_bytes = _write_stream(staged_specification, source, total_bytes)

        members, _specification, legacy_parameters = gather_validation_members(
            staged_specification,
            source_base_directory=base_directory,
            selected_specification_name=os.path.basename(
                os.path.abspath(specification_path)
            ),
        )
        if len(members) > REANA_SPEC_BUNDLE_MAX_FILES:
            raise REANAValidationError(
                "Specification bundle has too many files (maximum is {}).".format(
                    REANA_SPEC_BUNDLE_MAX_FILES
                )
            )
        for member in sorted(members):
            if member == CANONICAL_REANA_SPECIFICATION:
                continue
            destination = os.path.join(absolute_path, *member.split("/"))
            destination_directory = os.path.dirname(destination)
            os.makedirs(destination_directory, mode=0o2750, exist_ok=True)
            os.chmod(destination_directory, 0o2750)
            source_relative_path = os.path.relpath(
                os.path.abspath(members[member]), base_directory
            ).replace(os.sep, "/")
            descriptor = open_regular_file_beneath(
                base_directory, source_relative_path, "validation snapshot"
            )
            with os.fdopen(descriptor, "rb") as source:
                total_bytes = _write_stream(destination, source, total_bytes)
        return absolute_path, relative_path, total_bytes, legacy_parameters
    except Exception:
        shutil.rmtree(absolute_path, ignore_errors=True)
        raise


def workspace_seed_members(specification_path: str) -> Tuple[Dict[str, str], int]:
    """Return the declared workspace seed and its exact current byte size."""
    members, _specification, _legacy_parameters = gather_workspace_seed_members(
        specification_path
    )
    base_directory = os.path.dirname(os.path.abspath(specification_path))
    total = 0
    for member in members:
        source_relative_path = os.path.relpath(
            os.path.abspath(members[member]), base_directory
        ).replace(os.sep, "/")
        descriptor = open_regular_file_beneath(
            base_directory, source_relative_path, "workspace seed"
        )
        try:
            total += os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)
    return members, total


def seed_workspace(members: Dict[str, str], workspace_path: str) -> int:
    """Copy a declared seed into an empty workspace without following links."""
    os.makedirs(workspace_path, exist_ok=True)
    specification_path = members.get(CANONICAL_REANA_SPECIFICATION)
    if specification_path is None:
        raise REANAValidationError("Workspace seed is missing canonical reana.yaml.")
    base_directory = os.path.dirname(os.path.abspath(specification_path))
    copied = 0
    for member in sorted(members):
        destination = os.path.join(workspace_path, *member.split("/"))
        # Validator staging above deliberately forces private modes. Workspace
        # content instead uses conventional base modes so
        # ``REANA_WORKFLOW_UMASK`` provides the group-write access required by
        # runtime containers.
        os.makedirs(os.path.dirname(destination), mode=0o777, exist_ok=True)
        source_relative_path = os.path.relpath(
            os.path.abspath(members[member]), base_directory
        ).replace(os.sep, "/")
        source_descriptor = open_regular_file_beneath(
            base_directory, source_relative_path, "workspace seed"
        )
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            destination_flags |= os.O_NOFOLLOW
        try:
            destination_descriptor = os.open(destination, destination_flags, 0o666)
        except Exception:
            os.close(source_descriptor)
            raise
        try:
            with os.fdopen(source_descriptor, "rb") as source, os.fdopen(
                destination_descriptor, "wb"
            ) as target:
                while True:
                    chunk = source.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    copied += len(chunk)
                    target.write(chunk)
        except Exception:
            try:
                os.unlink(destination)
            except OSError:
                pass
            raise
    return copied
