# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server workflow fetcher."""

from abc import ABC, abstractmethod
import os
import posixpath
import re
import resource
import selectors
import shutil
import stat
import subprocess
import time
from typing import Any, List, Mapping, Optional, Sequence
from urllib.parse import quote, quote_plus, unquote_to_bytes, urlparse
import zipfile

import requests
from requests.exceptions import HTTPError, Timeout, RequestException
import werkzeug.exceptions
import werkzeug.routing

from reana_commons.specification_paths import (
    SPECIFICATION_BUNDLE_MAX_DEPTH,
    SPECIFICATION_BUNDLE_MAX_PATH_BYTES,
)

from reana_server.config import (
    FETCHER_ALLOWED_GITLAB_HOSTNAMES,
    FETCHER_ALLOWED_SCHEMES,
    FETCHER_MAXIMUM_CLONE_SIZE,
    FETCHER_MAXIMUM_EXTRACTED_SIZE,
    FETCHER_MAXIMUM_FILE_SIZE,
    FETCHER_MAXIMUM_FILES,
    FETCHER_REQUEST_TIMEOUT,
    REGEX_CHARS_TO_REPLACE,
    WORKFLOW_SPEC_EXTENSIONS,
    WORKFLOW_SPEC_FILENAMES,
)
from reana_server.specification_bundles import preflight_zip_metadata

_GIT_CLONE_POLL_INTERVAL = 0.5
_FETCHER_MAXIMUM_DIRECTORIES = FETCHER_MAXIMUM_FILES * 2 + 1024
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_GIT_LS_REMOTE_GLOB_CHARACTERS = frozenset("*?[")
_GIT_LS_REMOTE_MAX_OUTPUT_BYTES = 1024 * 1024
_GIT_LS_REMOTE_OUTPUT_LINES_PER_CANDIDATE = 2
_GIT_OBJECT_ID_MAX_BYTES = 64


def _git_environment() -> dict:
    """Return the environment used to run Git without interactive prompts."""
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _kill_and_reap(process: subprocess.Popen) -> None:
    """Terminate the given process if it is still running and reap it."""
    if process.poll() is None:
        process.kill()
    process.wait()


class REANAFetcherError(Exception):
    """Workflow specification fetcher error."""

    def __init__(self, message):
        """Initialize REANAFetcherError exception."""
        self.message = message


class ParsedUrl:
    """Utility class to parse and get information about a given URL."""

    def __init__(self, url: str):
        """Initialize the ParsedUrl class.

        :param url: URL to be parsed.
        """
        self.original_url = url
        self._parsed_url = urlparse(url)
        self.path = self._parsed_url.path.rstrip("/")
        self.dirname, self.basename = os.path.split(self.path)
        self.basename_without_extension, self.extension = os.path.splitext(
            self.basename
        )
        self.hostname = self._parsed_url.hostname
        self.netloc = self._parsed_url.netloc
        self.scheme = self._parsed_url.scheme


class WorkflowFetcherBase(ABC):
    """Fetch the specification of a workflow."""

    def __init__(
        self, parsed_url: ParsedUrl, output_dir: str, spec: Optional[str] = None
    ):
        """Initialize the workflow specification fetcher.

        :param parsed_url: Parsed URL of the workflow specification to fetch.
        :param output_dir: Directory where all the data will be saved to.
        :param spec: Optional path to the workflow specification.
        """
        self._parsed_url = parsed_url
        self._output_dir = os.path.abspath(output_dir)
        self._spec = spec
        self._workflow_path = None
        self._workflow_root = self._output_dir

    @abstractmethod
    def fetch(self) -> None:
        """Fetch the workflow specification."""
        pass

    @abstractmethod
    def generate_workflow_name(self) -> str:
        """Generate a workflow name from the given URL.

        :returns: Generated workflow name.
        """
        pass

    @staticmethod
    def _clean_workflow_name(name: str) -> str:
        """Replace invalid characters in the provided workflow name with dashes.

        :param name: Workflow name to be cleaned.
        :returns: Prettified workflow name.
        """
        return REGEX_CHARS_TO_REPLACE.sub("-", name).strip("-")

    @staticmethod
    def _download_file(url: str, output_path: str):
        """Download the given URL.

        This method also checks that the file to be downloaded does not exceed the
        maximum file size allowed (``FETCHER_MAXIMUM_FILE_SIZE``).

        :param url: URL of the file to be downloaded.
        :param output_path: Path where the file will be downloaded to.
        """

        def write_to_file(response: requests.Response, output_path: str) -> int:
            """Write the response content to the given file.

            :param response: Response to be written to the output file.
            :param output_path: Path to the output file.
            :returns: Number of bytes read from the response content.
            """
            read_bytes = 0
            with open(output_path, "wb") as output_file:
                # Use the same chunk size of `urlretrieve`
                for chunk in response.iter_content(chunk_size=1024 * 8):
                    read_bytes += len(chunk)
                    output_file.write(chunk)
                    if read_bytes > FETCHER_MAXIMUM_FILE_SIZE:
                        break
            return read_bytes

        try:
            with requests.get(
                url, stream=True, timeout=FETCHER_REQUEST_TIMEOUT
            ) as response:
                response.raise_for_status()

                content_length = int(response.headers.get("Content-Length", 0))
                if content_length > FETCHER_MAXIMUM_FILE_SIZE:
                    raise REANAFetcherError("Maximum file size exceeded")

                read_bytes = write_to_file(response, output_path)

                if read_bytes > FETCHER_MAXIMUM_FILE_SIZE:
                    os.remove(output_path)
                    raise REANAFetcherError("Maximum file size exceeded")
        except HTTPError as e:
            error = f"Cannot fetch the workflow specification: {e.response.reason} ({response.status_code})"
            if response.status_code == 404:
                error = "Cannot find the given workflow specification"
            raise REANAFetcherError(error)
        except Timeout:
            raise REANAFetcherError(
                "Timed-out while fetching the workflow specification"
            )
        except RequestException:
            raise REANAFetcherError(
                "Something went wrong while fetching the workflow specification"
            )

    def _discover_workflow_specs(self, dir: Optional[str] = None) -> List[str]:
        """Discover if there is a workflow specification in the given directory.

        :param dir: Directory used for the search.
            If None, the output directory will be used.
        :returns: List of paths of possible specification files.
        """
        if dir is None:
            dir = self._output_dir

        specs = []
        for filename in WORKFLOW_SPEC_FILENAMES:
            path = os.path.join(dir, filename)
            if os.path.isfile(path):
                specs.append(path)
        return specs

    @staticmethod
    def _is_path_inside(path: str, base: str) -> bool:
        """Check whether a path is inside a base directory.

        :param path: Path to check.
        :param base: Base directory that should contain the path.
        :returns: ``True`` if the path is inside the base directory, ``False`` otherwise.
        """
        real_base_path = os.path.realpath(base)
        real_path = os.path.realpath(path)
        try:
            return os.path.commonpath([real_base_path, real_path]) == real_base_path
        except ValueError:
            return False

    def _is_path_inside_output_dir(self, path: str) -> bool:
        """Check if a file is inside the output directory.

        :param path: Absolute path to the file.
        :returns: ``True`` if the file is inside the output directory, ``False`` otherwise.
        """
        return self._is_path_inside(path, self._output_dir)

    def _resolve_workflow_root_path(self) -> str:
        """Resolve the selected workflow root directory.

        The workflow root defaults to the fetcher's output directory, but can point to a
        subdirectory when launching workflows from repository folder URLs.

        :returns: Absolute path to the selected workflow root directory.
        """
        workflow_root = self._output_dir
        if self._workflow_path:
            workflow_root = os.path.abspath(
                os.path.join(self._output_dir, self._workflow_path)
            )
            if not self._is_path_inside_output_dir(workflow_root):
                raise REANAFetcherError("Invalid path to the workflow directory")
            if not os.path.isdir(workflow_root):
                raise REANAFetcherError("Cannot find the given workflow directory")
        return workflow_root

    def workflow_root_path(self) -> str:
        """Get the path of the selected workflow root directory.

        :returns: Absolute path to the selected workflow root directory.
        """
        return self._workflow_root

    def workflow_spec_path(self) -> str:
        """Get the path of the workflow specification file.

        If the path to the specification file was provided, only that will be used to
        find the workflow specification inside the selected workflow root directory.
        Otherwise, the file will be searched in the workflow root directory. This
        method should be called after ``fetch``.

        :returns: Path of the workflow specification file.
        """
        if self._spec:
            workflow_root = self.workflow_root_path()
            spec_path = os.path.abspath(os.path.join(workflow_root, self._spec))
            if not self._is_path_inside(spec_path, workflow_root):
                raise REANAFetcherError("Invalid path to the workflow specification")
            if not os.path.isfile(spec_path):
                raise REANAFetcherError(
                    "Cannot find the provided workflow specification"
                )
            return spec_path

        specs = [
            os.path.abspath(path)
            for path in self._discover_workflow_specs(self.workflow_root_path())
        ]
        unique_specs = list(set(specs))
        if not unique_specs:
            raise REANAFetcherError("Workflow specification was not found")
        if len(unique_specs) > 1:
            raise REANAFetcherError("Multiple workflow specifications found")
        return unique_specs[0]


class WorkflowFetcherGit(WorkflowFetcherBase):
    """Fetch the specification of a workflow from a Git repository."""

    def __init__(
        self,
        parsed_url: ParsedUrl,
        output_dir: str,
        git_ref: Optional[str] = None,
        spec: Optional[str] = None,
    ):
        """Initialize the workflow specification fetcher.

        :param parsed_url: Parsed URL of the git repository containing the workflow specification.
        :param output_dir: Directory where all the data will be saved to.
        :param git_ref: Optional reference to a specific git branch/commit.
        :param spec: Optional path to the workflow specification.
        """
        super().__init__(parsed_url, output_dir, spec)
        self._git_ref = git_ref

    def fetch(self) -> None:
        """Fetch workflow specification from a Git repository."""
        clone_command = [
            "git",
            "clone",
            "--depth=1",
            "--no-single-branch",
            self._parsed_url.original_url,
            self._output_dir,
        ]
        if not self._run_bounded_git(clone_command):
            raise REANAFetcherError(
                "Cannot clone the given Git repository. Please check that the provided "
                "URL is correct and that the repository is publicly accessible."
            )

        if self._git_ref:
            fetch_command = [
                "git",
                "-C",
                self._output_dir,
                "fetch",
                "--depth=1",
                "origin",
                self._git_ref,
            ]
            checkout_command = [
                "git",
                "-C",
                self._output_dir,
                "checkout",
                "--detach",
                "FETCH_HEAD",
            ]
            if not self._run_bounded_git(fetch_command) or not self._run_bounded_git(
                checkout_command
            ):
                raise REANAFetcherError(
                    f'Cannot checkout the given Git reference "{self._git_ref}"'
                )

        shutil.rmtree(os.path.join(self._output_dir, ".git"))
        file_count = 0
        total_size = 0
        for root, directories, files in os.walk(
            self._output_dir, topdown=True, followlinks=False
        ):
            for directory in directories:
                path = os.path.join(root, directory)
                if os.path.islink(path):
                    raise REANAFetcherError(
                        "Remote source repositories may not contain symbolic links"
                    )
            for filename in files:
                path = os.path.join(root, filename)
                mode = os.lstat(path).st_mode
                if not stat.S_ISREG(mode):
                    raise REANAFetcherError(
                        "Remote source repositories may contain only regular files"
                    )
                file_count += 1
                total_size += os.lstat(path).st_size
                if file_count > FETCHER_MAXIMUM_FILES:
                    raise REANAFetcherError("Remote source contains too many files")
                if total_size > FETCHER_MAXIMUM_EXTRACTED_SIZE:
                    raise REANAFetcherError("Remote source extracted size exceeded")
        self._workflow_root = self._resolve_workflow_root_path()

    def _run_bounded_git(self, command: Sequence[str]) -> bool:
        """Run Git while bounding its temporary clone tree."""
        try:
            process = subprocess.Popen(
                command,
                env=_git_environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if hasattr(resource, "prlimit"):
                resource.prlimit(
                    process.pid,
                    resource.RLIMIT_FSIZE,
                    (FETCHER_MAXIMUM_CLONE_SIZE, FETCHER_MAXIMUM_CLONE_SIZE),
                )
        except (OSError, ValueError):
            if "process" in locals():
                _kill_and_reap(process)
            return False

        clone_file_limit = FETCHER_MAXIMUM_FILES * 2 + 1024
        deadline = time.monotonic() + FETCHER_REQUEST_TIMEOUT
        try:
            while process.poll() is None:
                if self._clone_tree_exceeds_limits(clone_file_limit, strict=False):
                    raise REANAFetcherError(
                        "Remote Git clone exceeded its temporary storage limit"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise REANAFetcherError("Remote Git clone timed out")
                try:
                    process.wait(timeout=min(_GIT_CLONE_POLL_INTERVAL, remaining))
                except subprocess.TimeoutExpired:
                    pass
        except Exception:
            _kill_and_reap(process)
            raise
        # A fast clone can finish between polling intervals. Check its final
        # on-disk state before the caller removes the temporary ``.git`` tree.
        if self._clone_tree_exceeds_limits(clone_file_limit, strict=True):
            raise REANAFetcherError(
                "Remote Git clone exceeded its temporary storage limit"
            )
        return process.returncode == 0

    def _clone_tree_exceeds_limits(self, file_limit: int, strict: bool) -> bool:
        """Return whether the current temporary Git tree exceeds its bounds."""
        file_count = 0
        directory_count = 0
        total_size = 0
        pending = [(self._output_dir, "")]
        while pending:
            root, relative_root = pending.pop()
            try:
                entries = os.scandir(root)
            except OSError as error:
                if strict:
                    raise REANAFetcherError(
                        "Could not inspect the completed remote Git clone"
                    ) from error
                continue
            with entries:
                for entry in entries:
                    relative_path = posixpath.join(relative_root, entry.name)
                    if (
                        len(relative_path.encode("utf-8"))
                        > SPECIFICATION_BUNDLE_MAX_PATH_BYTES
                        or len(relative_path.split("/"))
                        > SPECIFICATION_BUNDLE_MAX_DEPTH
                    ):
                        return True
                    try:
                        metadata = os.lstat(entry.path)
                    except OSError as error:
                        if strict:
                            raise REANAFetcherError(
                                "Could not inspect the completed remote Git clone"
                            ) from error
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        directory_count += 1
                        if directory_count > _FETCHER_MAXIMUM_DIRECTORIES:
                            return True
                        pending.append((entry.path, relative_path))
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        if strict:
                            return True
                        continue
                    file_count += 1
                    total_size += metadata.st_size
                    if (
                        file_count > file_limit
                        or total_size > FETCHER_MAXIMUM_CLONE_SIZE
                    ):
                        return True
        return False

    def generate_workflow_name(self) -> str:
        """Generate a workflow name from the given repository URL.

        The repository's name is used as the name for the workflow.
        If a Git reference is provided, it is appended to the workflow name.

        :returns: Generated workflow name.
        """
        repository_name = self._parsed_url.basename_without_extension
        if self._git_ref:
            workflow_name = f"{repository_name}-{self._git_ref}"
        else:
            workflow_name = repository_name
        return self._clean_workflow_name(workflow_name)


class WorkflowFetcherYaml(WorkflowFetcherBase):
    """Fetch the specification of a workflow from a given URL pointing to a YAML file."""

    def __init__(self, parsed_url: ParsedUrl, output_dir: str):
        """Initialize the workflow specification fetcher.

        :param parsed_url: Parsed URL of the workflow specification to fetch.
        :param output_dir: Directory where all the data will be saved to.
        """
        super().__init__(parsed_url, output_dir, spec=parsed_url.basename)

    def fetch(self) -> None:
        """Fetch workflow specification from a given URL."""
        workflow_spec_path = os.path.join(self._output_dir, self._spec)
        self._download_file(self._parsed_url.original_url, workflow_spec_path)

    def generate_workflow_name(self) -> str:
        """Generate a workflow name from the given URL to the YAML specification file.

        The workflow name is the path to the YAML specification file.

        :returns: Generated workflow name.
        """
        workflow_name = None
        if self._parsed_url.basename in WORKFLOW_SPEC_FILENAMES:
            # We omit the name of the specification file if it is standard
            # (e.g. `reana.yaml` or `reana.yml`)
            workflow_name = self._clean_workflow_name(self._parsed_url.dirname)
        if not workflow_name:
            workflow_name = self._clean_workflow_name(
                f"{self._parsed_url.dirname}-{self._parsed_url.basename_without_extension}"
            )
        return workflow_name


class WorkflowFetcherZip(WorkflowFetcherBase):
    """Fetch the specification of a workflow from a zip archive."""

    def __init__(
        self,
        parsed_url: ParsedUrl,
        output_dir: str,
        spec: Optional[str] = None,
        workflow_name: Optional[str] = None,
        workflow_path: Optional[str] = None,
    ):
        """Initialize the workflow specification fetcher.

        :param parsed_url: Parsed URL of the workflow specification to fetch.
        :param output_dir: Directory where all the data will be saved to.
        :param spec: Optional path to the workflow specification.
        :param workflow_name: Workflow name that overrides the workflow name generation.
        :param workflow_path: Optional path to the workflow directory inside the archive.
        """
        super().__init__(parsed_url, output_dir, spec)
        self._archive_name = self._parsed_url.basename
        self._workflow_path = workflow_path
        if workflow_name:
            self._workflow_name = self._clean_workflow_name(workflow_name)
        else:
            self._workflow_name = self._clean_workflow_name(
                self._parsed_url.basename_without_extension
            )

    def fetch(self) -> None:
        """Fetch workflow specification from a zip archive."""
        archive_path = os.path.join(self._output_dir, self._archive_name)
        self._download_file(self._parsed_url.original_url, archive_path)
        self.extract_archive(archive_path)
        self._workflow_root = self._resolve_workflow_root_path()

    @staticmethod
    def _validate_archive_entries(entries) -> None:
        """Validate remote ZIP metadata before creating any output files."""
        if len(entries) > FETCHER_MAXIMUM_FILES:
            raise REANAFetcherError(
                "Remote source contains too many archive entries "
                f"(maximum is {FETCHER_MAXIMUM_FILES})"
            )
        declared_size = 0
        names = set()
        file_names = set()
        directories = set()
        for entry in entries:
            name = entry.filename
            normalized = posixpath.normpath(name)
            if (
                not name
                or "\x00" in name
                or "\\" in name
                or name.startswith("/")
                or (len(name) >= 2 and name[1] == ":")
                or normalized in (".", "..")
                or normalized != name.rstrip("/")
                or any(part in ("", ".", "..") for part in normalized.split("/"))
            ):
                raise REANAFetcherError(
                    f"Remote source contains an unsafe path: {name}"
                )
            components = normalized.split("/")
            if (
                len(name.encode("utf-8")) > SPECIFICATION_BUNDLE_MAX_PATH_BYTES
                or len(components) > SPECIFICATION_BUNDLE_MAX_DEPTH
            ):
                raise REANAFetcherError(
                    f"Remote source path exceeds its metadata limits: {name}"
                )
            for index in range(1, len(components)):
                directories.add("/".join(components[:index]))
                if len(directories) > _FETCHER_MAXIMUM_DIRECTORIES:
                    raise REANAFetcherError(
                        "Remote source contains too many directories"
                    )
            if normalized in names:
                raise REANAFetcherError(
                    f"Remote source contains a duplicate path: {normalized}"
                )
            names.add(normalized)
            mode = (entry.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if entry.is_dir():
                if file_type not in (0, stat.S_IFDIR):
                    raise REANAFetcherError(
                        f"Remote source contains a non-directory entry: {name}"
                    )
                continue
            if entry.flag_bits & 0x1:
                raise REANAFetcherError(
                    "Encrypted remote source archives are not supported"
                )
            if file_type not in (0, stat.S_IFREG):
                raise REANAFetcherError(
                    f"Remote source contains a non-regular file: {name}"
                )
            file_names.add(normalized)
            declared_size += entry.file_size
            if declared_size > FETCHER_MAXIMUM_EXTRACTED_SIZE:
                raise REANAFetcherError("Remote source extracted size exceeded")
        for name in names:
            components = name.split("/")
            for index in range(1, len(components)):
                parent = "/".join(components[:index])
                if parent in file_names:
                    raise REANAFetcherError(
                        f"Remote source path is nested below a regular file: {parent}"
                    )

    def _extract_archive_entries(self, zip_file, entries) -> None:
        """Extract validated remote ZIP members using exclusive regular files."""
        extracted_size = 0
        for entry in entries:
            destination = os.path.join(
                self._output_dir, *entry.filename.rstrip("/").split("/")
            )
            try:
                if entry.is_dir():
                    os.makedirs(destination, mode=0o700, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(destination, flags, 0o600)
                with zip_file.open(entry, "r") as source, os.fdopen(
                    descriptor, "wb"
                ) as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        extracted_size += len(chunk)
                        if extracted_size > FETCHER_MAXIMUM_EXTRACTED_SIZE:
                            raise REANAFetcherError(
                                "Remote source extracted size exceeded"
                            )
                        output.write(chunk)
            except REANAFetcherError:
                if not entry.is_dir():
                    try:
                        os.unlink(destination)
                    except OSError:
                        pass
                raise
            except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
                if not entry.is_dir():
                    try:
                        os.unlink(destination)
                    except OSError:
                        pass
                raise REANAFetcherError(
                    f"Could not extract remote source entry {entry.filename}: {exc}"
                )

    def extract_archive(self, archive_path: str) -> None:
        """Safely extract an already downloaded archive into the output tree."""
        try:
            with open(archive_path, "rb") as archive_stream:
                preflight_zip_metadata(archive_stream, FETCHER_MAXIMUM_FILES)
                with zipfile.ZipFile(archive_stream, "r") as zip_file:
                    entries = zip_file.infolist()
                    self._validate_archive_entries(entries)
                    self._extract_archive_entries(zip_file, entries)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise REANAFetcherError(f"The provided zip file is not valid: {exc}")
        finally:
            try:
                os.remove(archive_path)
            except OSError:
                pass

        if not self._discover_workflow_specs():
            top_level_entries = [
                os.path.join(self._output_dir, entry)
                for entry in os.listdir(self._output_dir)
            ]
            # Some zip archives contain a single directory with all the files.
            if len(top_level_entries) == 1 and os.path.isdir(top_level_entries[0]):
                top_level_dir = top_level_entries[0]
                # Move all entries inside the top level directory
                # to the output directory.
                for entry in os.listdir(top_level_dir):
                    shutil.move(os.path.join(top_level_dir, entry), self._output_dir)
                os.rmdir(top_level_dir)

    def generate_workflow_name(self) -> str:
        """Generate a workflow name from the given URL to the zip archive.

        The name of the zip archive is used as the name of the workflow, unless a custom
        workflow name was specified when initializing the fetcher.

        :returns: Generated workflow name.
        """
        return self._workflow_name


def extract_streamed_zip_response(
    response,
    output_dir: str,
    spec: Optional[str] = None,
    workflow_name: str = "workflow",
) -> WorkflowFetcherZip:
    """Bound and safely extract a streamed provider ZIP response."""
    archive_path = os.path.join(output_dir, ".reana-source-archive.zip")
    downloaded = 0
    try:
        with open(archive_path, "xb") as archive:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > FETCHER_MAXIMUM_FILE_SIZE:
                    raise REANAFetcherError("Maximum file size exceeded")
                archive.write(chunk)
        fetcher = WorkflowFetcherZip(
            ParsedUrl("https://invalid.example/source.zip"),
            output_dir,
            spec=spec,
            workflow_name=workflow_name,
        )
        fetcher.extract_archive(archive_path)
        return fetcher
    finally:
        try:
            response.close()
        except Exception:
            pass
        try:
            os.remove(archive_path)
        except OSError:
            pass


def _match_url(parsed_url: ParsedUrl, rules: Sequence[str]) -> Mapping[str, Any]:
    """Match the URL's path using the provided rules.

    :param parsed_url: Parsed URL whose path needs to be matched.
    :param rules: URL rules used to parse the path of the given URL.
    :returns: The parsed path components.
    """
    # We use the routing capabilities of werkzeug to match the URL path
    urls = werkzeug.routing.Map(
        [werkzeug.routing.Rule(rule) for rule in rules],
        strict_slashes=False,
    ).bind(parsed_url.hostname)
    try:
        _, components = urls.match(parsed_url.path)
    except werkzeug.exceptions.HTTPException:
        raise ValueError(f"The provided {parsed_url.hostname} URL is not valid")
    return components


def _looks_like_git_sha(git_ref: str) -> bool:
    """Check whether the given git ref looks like a commit SHA."""
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", git_ref, re.IGNORECASE))


def _looks_like_full_git_sha(git_ref: str) -> bool:
    """Check whether the given git ref looks like a full commit SHA."""
    return bool(re.fullmatch(r"[0-9a-f]{40}", git_ref, re.IGNORECASE))


def _decode_provider_tree_path(tree_path: str) -> List[str]:
    """Decode and validate a GitHub/GitLab tree URL path segment-by-segment."""
    decoded_segments = []
    for encoded_segment in tree_path.split("/"):
        if _INVALID_PERCENT_ESCAPE.search(encoded_segment):
            raise REANAFetcherError("Invalid path to the workflow directory")
        try:
            decoded_segment = unquote_to_bytes(encoded_segment).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise REANAFetcherError("Invalid path to the workflow directory") from exc
        if (
            decoded_segment in ("", ".", "..")
            or "/" in decoded_segment
            or "\\" in decoded_segment
            or "\x00" in decoded_segment
        ):
            raise REANAFetcherError("Invalid path to the workflow directory")
        decoded_segments.append(decoded_segment)

    decoded_tree_path = "/".join(decoded_segments)
    if (
        len(decoded_segments) > SPECIFICATION_BUNDLE_MAX_DEPTH
        or len(decoded_tree_path.encode("utf-8")) > SPECIFICATION_BUNDLE_MAX_PATH_BYTES
    ):
        raise REANAFetcherError("Invalid path to the workflow directory")
    return decoded_segments


def _remote_ref_candidates(path_parts: Sequence[str]) -> Sequence[str]:
    """Return bounded literal remote refs that may match a tree path."""
    ref_candidates = []
    for idx in range(len(path_parts), 0, -1):
        git_ref = "/".join(path_parts[:idx])
        if git_ref == "HEAD":
            continue
        candidates = (f"refs/heads/{git_ref}", f"refs/tags/{git_ref}")
        ref_candidates.extend(
            candidate
            for candidate in candidates
            if not _GIT_LS_REMOTE_GLOB_CHARACTERS.intersection(candidate)
        )
    return tuple(dict.fromkeys(ref_candidates))


def _run_bounded_git_output(
    command: Sequence[str], max_output_bytes: int, max_output_lines: int
) -> bytes:
    """Run Git while incrementally enforcing stdout byte and line limits."""
    process = None
    output = bytearray()
    output_lines = 0

    try:
        process = subprocess.Popen(
            command,
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        if process.stdout is None:
            raise OSError("Cannot read Git standard output")

        deadline = time.monotonic() + FETCHER_REQUEST_TIMEOUT
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise subprocess.TimeoutExpired(command, FETCHER_REQUEST_TIMEOUT)

                events = selector.select(timeout=remaining_time)
                if not events:
                    continue

                for key, _ in events:
                    read_size = min(64 * 1024, max_output_bytes - len(output) + 1)
                    chunk = os.read(key.fd, read_size)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue

                    output.extend(chunk)
                    output_lines += chunk.count(b"\n")
                    if (
                        len(output) > max_output_bytes
                        or output_lines > max_output_lines
                    ):
                        raise REANAFetcherError(
                            "Git reference discovery output exceeded its limit"
                        )

        remaining_time = deadline - time.monotonic()
        if process.poll() is None:
            if remaining_time <= 0:
                raise subprocess.TimeoutExpired(command, FETCHER_REQUEST_TIMEOUT)
            process.wait(timeout=remaining_time)
    except (OSError, subprocess.TimeoutExpired, REANAFetcherError) as exc:
        if isinstance(exc, REANAFetcherError):
            raise
        raise REANAFetcherError(
            "Cannot resolve Git references from the given repository"
        ) from exc
    finally:
        # Reap the child on every exit path, including unexpected exceptions
        # and interpreter shutdown, before the pipe is closed.
        if process is not None:
            _kill_and_reap(process)
            if process.stdout is not None:
                process.stdout.close()

    if process.returncode != 0:
        raise REANAFetcherError(
            "Cannot resolve Git references from the given repository"
        )
    return bytes(output)


def _remote_refs(repository_url: str, ref_candidates: Sequence[str]) -> set[str]:
    """Get matching remote heads and tags from a bounded candidate set."""
    # ``ls-remote`` treats ref operands as glob patterns. Git ref names cannot
    # contain these metacharacters, so discard them before spawning Git. Since
    # literal operands still use tail matching, stdout is bounded independently.
    ref_candidates = tuple(
        dict.fromkeys(
            candidate
            for candidate in ref_candidates
            if not _GIT_LS_REMOTE_GLOB_CHARACTERS.intersection(candidate)
        )
    )
    if not ref_candidates:
        return set()

    max_output_lines = len(ref_candidates) * _GIT_LS_REMOTE_OUTPUT_LINES_PER_CANDIDATE
    max_ref_bytes = max(len(os.fsencode(ref)) for ref in ref_candidates)
    max_line_bytes = _GIT_OBJECT_ID_MAX_BYTES + 1 + max_ref_bytes + len(b"^{}\n")
    max_output_bytes = min(
        _GIT_LS_REMOTE_MAX_OUTPUT_BYTES, max_output_lines * max_line_bytes
    )
    output = _run_bounded_git_output(
        [
            "git",
            "ls-remote",
            "--heads",
            "--tags",
            "--",
            repository_url,
            *ref_candidates,
        ],
        max_output_bytes,
        max_output_lines,
    )

    candidate_refs = {os.fsencode(ref): ref for ref in ref_candidates}
    return {
        candidate_refs[parts[1]]
        for line in output.splitlines()
        for parts in [line.split(None, 1)]
        if len(parts) == 2
        and parts[1] in candidate_refs
        and not parts[1].endswith(b"^{}")
    }


def _resolve_provider_tree_path(
    repository_url: str, tree_path: str
) -> tuple[str, Optional[str], str]:
    """Resolve a GitHub/GitLab tree path into git ref and workflow subdirectory.

    Tree URLs have the form ``tree/<git_ref>[/path/to/workflow]``. Each URL
    segment is decoded exactly once before validation. Since Git refs can
    themselves contain slashes, choose the longest path prefix that resolves to
    a remote branch or tag and treat the remaining suffix as a workflow
    subdirectory. Abbreviated SHAs are accepted only after successful remote ref
    discovery proves no branch or tag takes precedence. Full-length SHAs can
    fall back when remote ref discovery fails, leaving the archive endpoint to
    resolve the commit. Symbolic ``HEAD`` is likewise used only after checking
    for a longer branch or tag and remains available if discovery fails.
    """
    path_parts = _decode_provider_tree_path(tree_path)
    decoded_tree_path = "/".join(path_parts)
    if len(path_parts) == 1:
        return path_parts[0], None, decoded_tree_path

    try:
        remote_refs = _remote_refs(repository_url, _remote_ref_candidates(path_parts))
    except REANAFetcherError:
        if _looks_like_full_git_sha(path_parts[0]):
            return path_parts[0], "/".join(path_parts[1:]), decoded_tree_path
        if path_parts[0] == "HEAD":
            return "HEAD", "/".join(path_parts[1:]), decoded_tree_path
        raise

    for idx in range(len(path_parts), 0, -1):
        git_ref = "/".join(path_parts[:idx])
        workflow_path = "/".join(path_parts[idx:]) or None
        if (
            f"refs/heads/{git_ref}" in remote_refs
            or f"refs/tags/{git_ref}" in remote_refs
        ):
            return git_ref, workflow_path, decoded_tree_path

    # Commit-SHA tree URLs may point to commits outside the initial shallow clone.
    # Preserve the previous behavior and let the archive endpoint resolve the SHA.
    if _looks_like_git_sha(path_parts[0]):
        return path_parts[0], "/".join(path_parts[1:]), decoded_tree_path
    if path_parts[0] == "HEAD":
        return "HEAD", "/".join(path_parts[1:]), decoded_tree_path

    raise REANAFetcherError(
        f'Cannot checkout the given Git reference "{decoded_tree_path}"'
    )


def _get_github_fetcher(
    parsed_url: ParsedUrl, output_dir: str, spec: Optional[str] = None
) -> WorkflowFetcherBase:
    """Parse a GitHub URL and return the correct fetcher.

    :param parsed_url: Parsed URL to a GitHub repository.
    :param output_dir: Directory where all the data fetched will be saved.
    :param spec: Optional path to the workflow specification.
    :returns: Workflow fetcher.
    """
    # There are four different GitHub URLs we are interested in:
    # 1. URL to a repository: /<user>/<repo>
    # 2. Git URL: /<user>/<repo>.git
    # 3. URL to a branch/commit/tag: /<user>/<repo>/tree/<git_ref>
    # 4. URL to a zip snapshot: /<user>/<repo>/archive/.../<git_ref>.zip
    components = _match_url(
        parsed_url,
        [
            "/<username>/<repository>/",
            "/<username>/<repository>.git/",
            "/<username>/<repository>/tree/<path:git_ref>",
            "/<username>/<repository>/archive/<path:zip_path>",
        ],
    )

    username = components["username"]
    repository = components["repository"]
    tree_path = components.get("git_ref")
    zip_path = components.get("zip_path")

    if zip_path:
        # The name of the zip file is the git commit/branch/tag
        git_ref = parsed_url.basename_without_extension
        workflow_name = f"{repository}-{git_ref}"
        return WorkflowFetcherZip(parsed_url, output_dir, spec, workflow_name)
    else:
        git_ref = None
        workflow_path = None
        if tree_path:
            repository_url = f"https://github.com/{username}/{repository}.git"
            git_ref, workflow_path, tree_path = _resolve_provider_tree_path(
                repository_url, tree_path
            )
        archive_ref = quote(git_ref or "HEAD", safe="/")
        archive_url = ParsedUrl(
            f"https://github.com/{username}/{repository}/archive/{archive_ref}.zip"
        )
        workflow_name = repository if not tree_path else f"{repository}-{tree_path}"
        return WorkflowFetcherZip(
            archive_url, output_dir, spec, workflow_name, workflow_path
        )


def _get_gitlab_fetcher(
    parsed_url: ParsedUrl, output_dir: str, spec: Optional[str] = None
) -> WorkflowFetcherBase:
    """Parse a GitLab URL and return the correct fetcher.

    :param parsed_url: Parsed URL to a GitLab repository.
    :param output_dir: Directory where all the data fetched will be saved.
    :param spec: Optional path to the workflow specification.
    :returns: Workflow fetcher.
    """
    # There are four different GitLab URLs we are interested in:
    # 1. URL to a repository: /<user>/<repo>
    # 2. Git URL: /<user>/<repo>.git
    # 3. URL to a branch/commit/tag: /<user>/<repo>/-/tree/<git_ref>
    # 4. URL to a zip snapshot: /<user>/<repo>/-/archive/.../<repo>-<git_ref>.zip
    # Note that GitLab supports recursive subgroups, so <user> can contain slashes
    components = _match_url(
        parsed_url,
        [
            "/<path:username>/<repository>/",
            "/<path:username>/<repository>.git/",
            "/<path:username>/<repository>/-/tree/<path:git_ref>",
            "/<path:username>/<repository>/-/archive/<path:zip_path>",
        ],
    )

    username = components["username"]
    repository = components["repository"]
    tree_path = components.get("git_ref")
    zip_path = components.get("zip_path")

    if zip_path:
        # The name of the zip file is composed of the repository name and
        # the git commit/branch/tag
        workflow_name = parsed_url.basename_without_extension
        return WorkflowFetcherZip(parsed_url, output_dir, spec, workflow_name)
    else:
        git_ref = None
        workflow_path = None
        if tree_path:
            repository_url = (
                f"https://{parsed_url.hostname}/{username}/{repository}.git"
            )
            git_ref, workflow_path, tree_path = _resolve_provider_tree_path(
                repository_url, tree_path
            )
        project = quote_plus(f"{username}/{repository}")
        archive_ref = quote(git_ref or "HEAD", safe="")
        archive_url = ParsedUrl(
            f"https://{parsed_url.hostname}/api/v4/projects/{project}/"
            f"repository/archive.zip?sha={archive_ref}"
        )
        workflow_name = repository if not tree_path else f"{repository}-{tree_path}"
        return WorkflowFetcherZip(
            archive_url, output_dir, spec, workflow_name, workflow_path
        )


def get_fetcher(
    launcher_url: str, output_dir: str, spec: Optional[str] = None
) -> WorkflowFetcherBase:
    """Select the correct workflow fetcher based on the given URL.

    :param launcher_url: URL of the workflow specification.
    :param output_dir: Directory where all the data fetched will be saved.
    :param spec: Optional path to the workflow specification.
    :returns: Workflow fetcher.
    """
    parsed_url = ParsedUrl(launcher_url)

    if parsed_url.scheme not in FETCHER_ALLOWED_SCHEMES:
        raise ValueError("URL scheme not allowed")

    if spec:
        _, spec_ext = os.path.splitext(spec)
        if spec_ext not in WORKFLOW_SPEC_EXTENSIONS:
            raise ValueError(
                "The provided specification doesn't have a valid file extension"
            )

    if parsed_url.netloc == "github.com":
        return _get_github_fetcher(parsed_url, output_dir, spec)
    elif parsed_url.netloc in FETCHER_ALLOWED_GITLAB_HOSTNAMES:
        return _get_gitlab_fetcher(parsed_url, output_dir, spec)
    elif parsed_url.extension == ".git":
        return WorkflowFetcherGit(parsed_url, output_dir, spec=spec)
    elif parsed_url.extension == ".zip":
        return WorkflowFetcherZip(parsed_url, output_dir, spec)
    elif parsed_url.extension in WORKFLOW_SPEC_EXTENSIONS:
        if spec:
            raise ValueError(
                "Cannot use the 'specification' argument when the URL points directly "
                "to a specification file"
            )
        return WorkflowFetcherYaml(parsed_url, output_dir)
    else:
        raise ValueError("Cannot handle given URL")
