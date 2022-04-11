# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server workflow fetcher."""

from abc import ABC, abstractmethod
import os
import shutil
from typing import Any, List, Mapping, Optional, Sequence
from urllib.parse import urlparse
import zipfile

from git import Repo
import requests
from requests.exceptions import HTTPError, Timeout, RequestException
import werkzeug.exceptions
import werkzeug.routing

from reana_server.config import (
    FETCHER_ALLOWED_GITLAB_HOSTNAMES,
    FETCHER_ALLOWED_SCHEMES,
    FETCHER_MAXIMUM_FILE_SIZE,
    FETCHER_REQUEST_TIMEOUT,
    REGEX_CHARS_TO_REPLACE,
    WORKFLOW_SPEC_EXTENSIONS,
    WORKFLOW_SPEC_FILENAMES,
)


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
        try:
            repository = Repo.clone_from(
                self._parsed_url.original_url,
                self._output_dir,
                depth=1,
                no_single_branch=True,
                env={"GIT_TERMINAL_PROMPT": "0"},
            )
        except Exception:
            raise REANAFetcherError(
                "Cannot clone the given Git repository. Please check that the provided "
                "URL is correct and that the repository is publicly accessible."
            )

        if self._git_ref:
            try:
                repository.remote().fetch(self._git_ref, depth=1)
                repository.git.checkout(self._git_ref)
            except Exception:
                raise REANAFetcherError(
                    f'Cannot checkout the given Git reference "{self._git_ref}"'
                )

        shutil.rmtree(os.path.join(self._output_dir, ".git"))

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
        try:
            with zipfile.ZipFile(archive_path, "r") as zip_file:
                zip_file.extractall(path=self._output_dir)
        except zipfile.BadZipfile:
            raise REANAFetcherError("The provided zip file is not valid")

        os.remove(archive_path)

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
        repository_url = ParsedUrl(f"https://github.com/{username}/{repository}.git")
        return WorkflowFetcherGit(repository_url, output_dir, git_ref, spec)


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
        repository_url = ParsedUrl(
            f"https://{parsed_url.hostname}/{username}/{repository}.git"
        )
        return WorkflowFetcherGit(repository_url, output_dir, git_ref, spec)


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
