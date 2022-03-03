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
from typing import List, Optional
from urllib.parse import urlparse
from urllib.request import urlretrieve
import zipfile

from git import Repo

from reana_server.config import WORKFLOW_SPEC_EXTENSIONS, WORKFLOW_SPEC_FILENAMES


class WorkflowFetcherBase(ABC):
    """Fetch the specification of a workflow."""

    def __init__(self, url: str, output_dir: str, spec: Optional[str] = None):
        """Initialize the workflow specification fetcher.

        :param url: URL of the workflow specification to fetch.
        :param output_dir: Directory where all the data will be saved to.
        :param spec: Optional path to the workflow specification.
        """
        self._url = url
        self._output_dir = os.path.abspath(output_dir)
        self._spec = spec

    @abstractmethod
    def fetch(self) -> None:
        """Fetch the workflow specification."""
        pass

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
                raise Exception("Invalid path to the workflow specification")
            if not os.path.isfile(spec_path):
                raise Exception("Cannot find provided workflow specification")
            return spec_path

        specs = [os.path.abspath(path) for path in self._discover_workflow_specs()]
        unique_specs = list(set(specs))
        if not unique_specs:
            raise Exception("Workflow specification was not found")
        if len(unique_specs) > 1:
            raise Exception("Multiple workflow specifications found")
        return unique_specs[0]


class WorkflowFetcherGit(WorkflowFetcherBase):
    """Fetch the specification of a workflow from a Git repository."""

    def __init__(
        self,
        url: str,
        output_dir: str,
        git_ref: Optional[str] = None,
        spec: Optional[str] = None,
    ):
        """Initialize the workflow specification fetcher.

        :param url: URL of the git repository containing the workflow specification.
        :param output_dir: Directory where all the data will be saved to.
        :param git_ref: Optional reference to a specific git branch/commit.
        :param spec: Optional path to the workflow specification.
        """
        super().__init__(url, output_dir, spec)
        self._git_ref = git_ref

    def fetch(self) -> None:
        """Fetch workflow specification from a Git repository."""
        repository = Repo.clone_from(self._url, self._output_dir, depth=1)
        if self._git_ref:
            repository.remote().fetch(self._git_ref, depth=1)
            repository.git.checkout(self._git_ref)


class WorkflowFetcherYaml(WorkflowFetcherBase):
    """Fetch the specification of a workflow from a given URL pointing to a YAML file."""

    def __init__(self, url: str, output_dir: str, spec_name: str):
        """Initialize the workflow specification fetcher.

        :param url: URL of the workflow specification to fetch.
        :param output_dir: Directory where all the data will be saved to.
        :param spec_name: Filename of the workflow specification file to be fetched.
        """
        super().__init__(url, output_dir, spec_name)

    def fetch(self) -> None:
        """Fetch workflow specification from a given URL."""
        workflow_spec_path = os.path.join(self._output_dir, self._spec)
        urlretrieve(self._url, workflow_spec_path)


class WorkflowFetcherZip(WorkflowFetcherBase):
    """Fetch the specification of a workflow from a zip archive."""

    def __init__(self, url: str, output_dir: str, archive_name: str):
        """Initialize the workflow specification fetcher.

        :param url: URL of the workflow specification to fetch.
        :param output_dir: Directory where all the data will be saved to.
        :param archive_name: Filename of the zip archive to be fetched.
        """
        super().__init__(url, output_dir)
        self._archive_name = archive_name

    def fetch(self) -> None:
        """Fetch workflow specification from a zip archive."""
        archive_path = os.path.join(self._output_dir, self._archive_name)
        urlretrieve(self._url, archive_path)
        with zipfile.ZipFile(archive_path, "r") as zip_file:
            zip_file.extractall(path=self._output_dir)
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


def _get_github_fetcher(url: str, output_dir: str) -> WorkflowFetcherGit:
    """Parse a GitHub URL and return the correct fetcher.

    :param url: URL to a GitHub repository.
    :param output_dir: Directory where all the data fetched will be saved.
    :returns: Workflow fetcher.
    """
    # There are four different GitHub URLs:
    # 1. URL to a repository: /<user>/<repo>
    # 2. URL to a branch/commit: /<user>/<repo>/tree/<git_ref>
    # 3. URL to a specific folder: /<user>/<repo>/tree/<git_ref>/<path_to_dir>
    # 4. URL to a specific file: /<user>/<repo>/blob/<git_ref>/<path_to_file>

    # We are interested in five components: username, repository, tree/blob, git_ref, path
    url_path_components = ["username", "repository", "tree_or_blob", "git_ref", "path"]
    parsed_url = urlparse(url)
    split_url_path = parsed_url.path.strip("/").split(
        "/", maxsplit=len(url_path_components) - 1
    )
    components = {k: v for k, v in zip(url_path_components, split_url_path)}

    username = components.get("username")
    repository = components.get("repository")
    tree_or_blob = components.get("tree_or_blob")
    git_ref = components.get("git_ref")
    path = components.get("path")

    if not username:
        raise ValueError("Username not provided in GitHub URL")
    if not repository:
        raise ValueError("Repository not provided in GitHub URL")
    if tree_or_blob and tree_or_blob not in ["tree", "blob"]:
        raise ValueError("Malformed GitHub URL")
    if tree_or_blob and not git_ref:
        raise ValueError("Branch or commit ID not provided in GitHub URL")
    if tree_or_blob == "blob" and not path:
        raise ValueError("Specification file not provided in GitHub URL")
    if tree_or_blob == "blob":
        _, extension = os.path.splitext(path)
        if extension not in WORKFLOW_SPEC_EXTENSIONS:
            raise ValueError("GitHub URL points to an invalid specification file")
    if tree_or_blob == "tree" and path:
        raise ValueError("GitHub URL points to a directory")

    repository_url = f"https://github.com/{username}/{repository}.git"
    return WorkflowFetcherGit(repository_url, output_dir, git_ref, spec=path)


def get_fetcher(url: str, output_dir: str) -> WorkflowFetcherBase:
    """Select the correct workflow fetcher based on the given URL.

    :param url: URL of the workflow specification.
    :param output_dir: Directory where all the data fetched will be saved.
    :returns: Workflow fetcher.
    """
    parsed_url = urlparse(url)
    _, extension = os.path.splitext(parsed_url.path)
    basename = os.path.basename(parsed_url.path)

    if extension == ".git":
        return WorkflowFetcherGit(url, output_dir)
    elif parsed_url.netloc == "github.com":
        return _get_github_fetcher(url, output_dir)
    elif extension in WORKFLOW_SPEC_EXTENSIONS:
        return WorkflowFetcherYaml(url, output_dir, spec_name=basename)
    elif extension == ".zip":
        return WorkflowFetcherZip(url, output_dir, archive_name=basename)
    else:
        raise ValueError("Cannot handle given url")
