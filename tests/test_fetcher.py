# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server workflow fetcher tests."""

import os
import pytest
from unittest.mock import Mock, patch
import zipfile

from git import Repo

from reana_server.fetcher import (
    _get_github_fetcher,
    get_fetcher,
    WorkflowFetcherGit,
    WorkflowFetcherYaml,
    WorkflowFetcherZip,
)

GIT_URL = "https://github.com/reanahub/reana-demo-root6-roofit.git"
GITHUB_REPO_URL = "https://github.com/reanahub/reana-demo-root6-roofit"
ZENODO_URL = "https://zenodo.org/record/5752285/files/circular-health-data-processing-master.zip?download=1"
YAML_URL = "https://raw.githubusercontent.com/reanahub/reana-demo-root6-roofit/master/reana.yaml"


@pytest.mark.parametrize(
    "url, expected_fetcher_class",
    [
        (GIT_URL, WorkflowFetcherGit),
        (GITHUB_REPO_URL, WorkflowFetcherGit),
        (ZENODO_URL, WorkflowFetcherZip),
        (YAML_URL, WorkflowFetcherYaml),
        pytest.param(
            "https://reana.io",
            None,
            marks=pytest.mark.xfail(raises=ValueError, strict=True),
        ),
    ],
)
def test_fetcher_selection(url, expected_fetcher_class, tmp_path):
    """Test selection of the fetcher based on the provided URL."""
    assert isinstance(get_fetcher(url, tmp_path), expected_fetcher_class)


@pytest.mark.parametrize("with_git_ref", [True, False])
def test_fetcher_git(with_git_ref, tmp_path):
    """Test fetching the workflow specification from a git repository."""

    def create_git_repository(repo_path, files):
        """Create a git repository with one commit for each file."""
        repository = Repo.init(repo_path)

        commits = []
        for file, content in files:
            file_path = os.path.join(repo_path, file)
            with open(file_path, "w") as f:
                f.write(content)
            repository.index.add(file_path)
            commit = repository.index.commit(f"Add {file}")
            commits.append(commit.hexsha)

        return commits

    repo_dir = os.path.join(tmp_path, "repo")
    output_dir = os.path.join(tmp_path, "output")

    files = [
        ("reana.yaml", "Content of reana.yaml"),
        ("README.md", "# Test Git Repository"),
    ]
    commits = create_git_repository(repo_dir, files)

    git_ref = commits[0] if with_git_ref else None
    fetcher = WorkflowFetcherGit(repo_dir, output_dir, git_ref)
    fetcher.fetch()
    expected_path = os.path.join(output_dir, "reana.yaml")
    assert expected_path == fetcher.workflow_spec_path()
    assert os.path.isfile(expected_path)


@pytest.mark.parametrize(
    "spec_name", ["reana.yaml", "reana.yml", "reana-snakemake.yaml"]
)
def test_fetcher_yaml(spec_name, tmp_path):
    """Test fetching the workflow specification file from a URL."""

    input_dir = os.path.join(tmp_path, "input")
    os.makedirs(input_dir)
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)

    spec_path = os.path.join(input_dir, spec_name)
    with open(spec_path, "w") as f:
        f.write("Content of reana.yaml")

    fetcher = get_fetcher(f"file://{spec_path}", output_dir)
    assert isinstance(fetcher, WorkflowFetcherYaml)
    fetcher.fetch()
    expected_path = os.path.join(output_dir, spec_name)
    assert expected_path == fetcher.workflow_spec_path()
    assert os.path.isfile(expected_path)


@pytest.mark.parametrize("with_top_level_dir", [True, False])
def test_fetcher_zip(with_top_level_dir, tmp_path):
    """Test fetching the workflow specification from a zip archive."""

    def create_zip_file(archive_path, files):
        """Create a zip archive with the given files."""
        with zipfile.ZipFile(archive_path, "w") as zip_file:
            for file, content in files:
                zip_file.writestr(file, content)

    input_dir = os.path.join(tmp_path, "input")
    os.makedirs(input_dir)
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)

    archive_path = os.path.join(input_dir, "archive.zip")
    if with_top_level_dir:
        files = [("dir/reana.yaml", "Content of reana.yaml")]
    else:
        files = [("reana.yaml", "Content of reana.yaml")]
    create_zip_file(archive_path, files)

    fetcher = get_fetcher(f"file://{archive_path}", output_dir)
    assert isinstance(fetcher, WorkflowFetcherZip)
    fetcher.fetch()
    expected_path = os.path.join(output_dir, "reana.yaml")
    assert expected_path == fetcher.workflow_spec_path()
    assert os.path.isfile(expected_path)


@pytest.mark.parametrize(
    "url, username, repository, git_ref, spec",
    [
        ("https://github.com/user/repo", "user", "repo", None, None),
        ("https://github.com/user/repo/tree/branch", "user", "repo", "branch", None),
        (
            "https://github.com/user/repo/blob/branch/path/to/reana.yaml",
            "user",
            "repo",
            "branch",
            "path/to/reana.yaml",
        ),
        (
            "https://github.com/user/repo/blob/branch/path/to/reana.yml",
            "user",
            "repo",
            "branch",
            "path/to/reana.yml",
        ),
    ],
)
def test_github_fetcher(url, username, repository, git_ref, spec, tmp_path):
    """Test creating a valid fetcher for GitHub URLs."""
    mock_git_fetcher = Mock()
    with patch("reana_server.fetcher.WorkflowFetcherGit", mock_git_fetcher):
        _get_github_fetcher(url, tmp_path)
        mock_git_fetcher.assert_called_once_with(
            f"https://github.com/{username}/{repository}.git",
            tmp_path,
            git_ref,
            spec=spec,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/user/repo/invalid",
        "https://github.com/user/repo/tree/branch/path/to/dir",
        "https://github.com/user/repo/blob/branch/path/to/file.txt",
        "https://github.com/user",
        "https://github.com/",
    ],
)
@pytest.mark.xfail(raises=ValueError, strict=True)
def test_invalid_github_fetcher(url, tmp_path):
    """Test handling of invalid GitHub URLs."""
    _get_github_fetcher(url, tmp_path)
