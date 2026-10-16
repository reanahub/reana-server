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
import resource
import shutil
import stat
import subprocess
import time
from typing import Any, List, Mapping, Optional, Sequence
from urllib.parse import quote, quote_plus, urlparse
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

    def _is_path_inside_output_dir(self, path: str) -> bool:
        """Check if a file is inside the output directory.

        :param path: Absolute path to the file.
        :returns: ``True`` if the file is inside the output directory, ``False`` otherwise.
        """
        real_output_dir = os.path.realpath(self._output_dir)
        real_file_path = os.path.realpath(path)
        return os.path.commonpath([real_output_dir, real_file_path]) == real_output_dir

    def workflow_spec_path(self) -> str:
        """Get the path of the workflow specification file.

        If the path to the specification file was provided, only that will be used to
        find the workflow specification. Otherwise, the file will be searched in the
        output directory. This method should be called after ``fetch``.

        :returns: Path of the workflow specification file.
        """
        if self._spec:
            spec_path = os.path.abspath(os.path.join(self._output_dir, self._spec))
            if not self._is_path_inside_output_dir(spec_path):
                raise REANAFetcherError("Invalid path to the workflow specification")
            if not os.path.isfile(spec_path):
                raise REANAFetcherError(
                    "Cannot find the provided workflow specification"
                )
            return spec_path

        specs = [os.path.abspath(path) for path in self._discover_workflow_specs()]
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

    def _run_bounded_git(self, command: Sequence[str]) -> bool:
        """Run Git while bounding its temporary clone tree."""
        environment = dict(os.environ)
        environment["GIT_TERMINAL_PROMPT"] = "0"

        def kill_and_reap(process) -> None:
            if process.poll() is None:
                process.kill()
            process.wait()

        try:
            process = subprocess.Popen(
                command,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            resource.prlimit(
                process.pid,
                resource.RLIMIT_FSIZE,
                (FETCHER_MAXIMUM_CLONE_SIZE, FETCHER_MAXIMUM_CLONE_SIZE),
            )
        except (OSError, ValueError):
            if "process" in locals():
                kill_and_reap(process)
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
            kill_and_reap(process)
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
    ):
        """Initialize the workflow specification fetcher.

        :param parsed_url: Parsed URL of the workflow specification to fetch.
        :param output_dir: Directory where all the data will be saved to.
        :param spec: Optional path to the workflow specification.
        :param workflow_name: Workflow name that overrides the workflow name generation.
        """
        super().__init__(parsed_url, output_dir, spec)
        self._archive_name = self._parsed_url.basename
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
            except UnicodeError:
                # The archive is valid, but the file name cannot be stored
                # using the filesystem encoding of the running interpreter.
                raise REANAFetcherError(
                    "Could not extract remote source entry "
                    f"{ascii(entry.filename)}: the file name cannot be "
                    "represented in the filesystem encoding of the server"
                )
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
    git_ref = components.get("git_ref")
    zip_path = components.get("zip_path")

    if zip_path:
        # The name of the zip file is the git commit/branch/tag
        git_ref = parsed_url.basename_without_extension
        workflow_name = f"{repository}-{git_ref}"
        return WorkflowFetcherZip(parsed_url, output_dir, spec, workflow_name)
    else:
        archive_ref = quote(git_ref or "HEAD", safe="/")
        archive_url = ParsedUrl(
            f"https://github.com/{username}/{repository}/archive/{archive_ref}.zip"
        )
        workflow_name = repository if not git_ref else f"{repository}-{git_ref}"
        return WorkflowFetcherZip(archive_url, output_dir, spec, workflow_name)


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
    git_ref = components.get("git_ref")
    zip_path = components.get("zip_path")

    if zip_path:
        # The name of the zip file is composed of the repository name and
        # the git commit/branch/tag
        workflow_name = parsed_url.basename_without_extension
        return WorkflowFetcherZip(parsed_url, output_dir, spec, workflow_name)
    else:
        project = quote_plus(f"{username}/{repository}")
        archive_ref = quote(git_ref or "HEAD", safe="")
        archive_url = ParsedUrl(
            f"https://{parsed_url.hostname}/api/v4/projects/{project}/"
            f"repository/archive.zip?sha={archive_ref}"
        )
        workflow_name = repository if not git_ref else f"{repository}-{git_ref}"
        return WorkflowFetcherZip(archive_url, output_dir, spec, workflow_name)


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
