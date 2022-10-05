# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server workflow fetcher tests."""

import os
from urllib.request import urlretrieve
import pytest
from unittest.mock import MagicMock, Mock, patch
import zipfile

from git import Repo

from reana_server.fetcher import (
    _get_github_fetcher,
    _get_gitlab_fetcher,
    get_fetcher,
    ParsedUrl,
    REANAFetcherError,
    WorkflowFetcherBase,
    WorkflowFetcherGit,
    WorkflowFetcherYaml,
    WorkflowFetcherZip,
)

GIT_URL = "https://github.com/reanahub/reana-demo-root6-roofit.git"
GITHUB_REPO_URL = "https://github.com/reanahub/reana-demo-root6-roofit"
GITHUB_REPO_ZIP = (
    "https://github.com/reanahub/reana-demo-root6-roofit/archive/refs/heads/master.zip"
)
GITLAB_REPO_URL = "https://gitlab.com/group/user/repo"
GITLAB_REPO_ZIP = (
    "https://gitlab.cern.ch/group/user/repo/-/archive/master/repo-master.zip"
)
ZENODO_URL = "https://zenodo.org/record/5752285/files/circular-health-data-processing-master.zip?download=1"
YAML_URL = "https://raw.githubusercontent.com/reanahub/reana-demo-root6-roofit/master/reana.yaml"


@pytest.mark.parametrize(
    "url, expected_fetcher_class",
    [
        (GIT_URL, WorkflowFetcherGit),
        (GIT_URL + "/", WorkflowFetcherGit),
        (GITHUB_REPO_URL, WorkflowFetcherGit),
        (GITHUB_REPO_URL + "/", WorkflowFetcherGit),
        (GITHUB_REPO_ZIP, WorkflowFetcherZip),
        (GITLAB_REPO_URL, WorkflowFetcherGit),
        (GITLAB_REPO_URL + "/", WorkflowFetcherGit),
        (GITLAB_REPO_ZIP, WorkflowFetcherZip),
        (ZENODO_URL, WorkflowFetcherZip),
        (YAML_URL, WorkflowFetcherYaml),
        pytest.param(
            "https://reana.io",
            None,
            marks=pytest.mark.xfail(raises=ValueError, strict=True),
        ),
        pytest.param(
            "ftp://reana.io/reana.yaml",
            None,
            marks=pytest.mark.xfail(raises=ValueError, strict=True),
        ),
    ],
)
def test_fetcher_selection(url, expected_fetcher_class, tmp_path):
    """Test selection of the fetcher based on the provided URL."""
    assert isinstance(get_fetcher(url, tmp_path), expected_fetcher_class)


@pytest.mark.parametrize(
    "with_git_ref, spec",
    [
        (None, None),
        ("commit", None),
        ("branch", None),
        ("tag", None),
        (None, "reana-cwl.yaml"),
        ("commit", "reana-cwl.yaml"),
        ("branch", "reana-cwl.yaml"),
        ("tag", "reana-cwl.yaml"),
        pytest.param(
            "commit",
            "reana-not-present.yaml",
            marks=pytest.mark.xfail(raises=REANAFetcherError, strict=True),
        ),
        pytest.param(
            "branch",
            "reana-not-present.yaml",
            marks=pytest.mark.xfail(raises=REANAFetcherError, strict=True),
        ),
        pytest.param(
            "tag",
            "reana-not-present.yaml",
            marks=pytest.mark.xfail(raises=REANAFetcherError, strict=True),
        ),
        pytest.param(
            None,
            "invalid.yaml",
            marks=pytest.mark.xfail(raises=REANAFetcherError, strict=True),
        ),
    ],
)
def test_fetcher_git(with_git_ref, spec, tmp_path):
    """Test fetching the workflow specification from a git repository."""

    def create_git_repository(repo_path, files, idx):
        """Create a git repository with one commit for each file."""
        repository = Repo.init(repo_path, initial_branch="main")

        commits = []
        for file, content in files:
            file_path = os.path.join(repo_path, file)
            with open(file_path, "w") as f:
                f.write(content)
            repository.index.add(file_path)
            commit = repository.index.commit(f"Add {file}")
            commits.append(commit.hexsha)

        # Checkout given commit
        repository.git.checkout(commits[idx])
        # Create branch
        repository.create_head("new-branch")
        # Create tag
        repository.create_tag("new-tag")
        # Go back to main branch
        repository.git.checkout("main")

        return commits

    repo_dir = os.path.join(tmp_path, "repo")
    output_dir = os.path.join(tmp_path, "output")

    files = [
        ("reana.yaml", "Content of reana.yaml"),
        ("reana-cwl.yaml", "Content of reana-cwl.yaml"),
        ("README.md", "# Test Git Repository"),
        ("reana-not-present.yaml", "Content of reana-not-present.yaml"),
    ]

    commits = create_git_repository(repo_dir, files, idx=1)

    if with_git_ref == "branch":
        git_ref = "new-branch"
    elif with_git_ref == "commit":
        git_ref = commits[1]
    elif with_git_ref == "tag":
        git_ref = "new-tag"
    else:
        assert with_git_ref is None
        git_ref = None

    fetcher = WorkflowFetcherGit(
        ParsedUrl(f"file://{repo_dir}"), output_dir, git_ref, spec
    )
    fetcher.fetch()
    expected_path = os.path.join(output_dir, spec or "reana.yaml")
    assert expected_path == fetcher.workflow_spec_path()
    assert os.path.isfile(expected_path)


@pytest.mark.parametrize(
    "spec_name, spec_argument",
    [
        ("reana.yaml", None),
        ("reana.yml", None),
        ("reana-snakemake.yaml", None),
        pytest.param(
            "reana.yaml",
            "reana-snakemake.yaml",
            marks=pytest.mark.xfail(raises=ValueError, strict=True),
        ),
        pytest.param(
            "invalid.txt", None, marks=pytest.mark.xfail(raises=ValueError, strict=True)
        ),
    ],
)
@patch("reana_server.fetcher.FETCHER_ALLOWED_SCHEMES", ["file"])
def test_fetcher_yaml(spec_name, spec_argument, tmp_path):
    """Test fetching the workflow specification file from a URL."""

    input_dir = os.path.join(tmp_path, "input")
    os.makedirs(input_dir)
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)

    spec_path = os.path.join(input_dir, spec_name)
    with open(spec_path, "w") as f:
        f.write("Content of reana.yaml")

    mock_download = Mock()
    mock_download.side_effect = urlretrieve
    with patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file", mock_download
    ):
        fetcher = get_fetcher(f"file://{spec_path}", output_dir, spec_argument)
        assert isinstance(fetcher, WorkflowFetcherYaml)
        fetcher.fetch()
        expected_path = os.path.join(output_dir, spec_name)
        assert expected_path == fetcher.workflow_spec_path()
        assert os.path.isfile(expected_path)


@pytest.mark.parametrize(
    "with_top_level_dir, spec",
    [
        (True, None),
        (True, "reana-cwl.yaml"),
        (False, None),
        (False, "reana-cwl.yaml"),
        pytest.param(
            True, "invalid.txt", marks=pytest.mark.xfail(raises=ValueError, strict=True)
        ),
        pytest.param(
            True,
            "invalid.yaml",
            marks=pytest.mark.xfail(raises=REANAFetcherError, strict=True),
        ),
    ],
)
@patch("reana_server.fetcher.FETCHER_ALLOWED_SCHEMES", ["file"])
def test_fetcher_zip(with_top_level_dir, spec, tmp_path):
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
        files = [
            ("dir/reana.yaml", "Content of reana.yaml"),
            ("dir/reana-cwl.yaml", "Content of reana-cwl.yaml"),
        ]
    else:
        files = [
            ("reana.yaml", "Content of reana.yaml"),
            ("reana-cwl.yaml", "Content of reana-cwl.yaml"),
        ]
    create_zip_file(archive_path, files)

    mock_download = Mock()
    mock_download.side_effect = urlretrieve
    with patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file", mock_download
    ):
        fetcher = get_fetcher(f"file://{archive_path}", output_dir, spec)
        assert isinstance(fetcher, WorkflowFetcherZip)
        fetcher.fetch()
        expected_path = os.path.join(output_dir, spec or "reana.yaml")
        assert expected_path == fetcher.workflow_spec_path()
        assert os.path.isfile(expected_path)


@pytest.mark.parametrize(
    "url, username, repository, git_ref",
    [
        ("https://github.com/user/repo", "user", "repo", None),
        ("https://github.com/user/repo/", "user", "repo", None),
        ("https://github.com/user/repo.git", "user", "repo", None),
        ("https://github.com/user/repo.git/", "user", "repo", None),
        ("https://github.com/user/repo/tree/branch", "user", "repo", "branch"),
        ("https://github.com/user/repo/tree/branch/", "user", "repo", "branch"),
        (
            "https://github.com/user/repo/tree/tag/with/slashes",
            "user",
            "repo",
            "tag/with/slashes",
        ),
        (
            "https://github.com/user/repo/tree/tag/with/slashes/",
            "user",
            "repo",
            "tag/with/slashes",
        ),
    ],
)
def test_github_fetcher(url, username, repository, git_ref, tmp_path):
    """Test creating a valid fetcher for GitHub URLs."""
    mock_git_fetcher = Mock()
    with patch("reana_server.fetcher.WorkflowFetcherGit", mock_git_fetcher):
        _get_github_fetcher(ParsedUrl(url), tmp_path)
        mock_git_fetcher.assert_called_once()
        expected_repo_url = f"https://github.com/{username}/{repository}.git"
        (
            call_parsed_url,
            call_tmp_path,
            call_git_ref,
            call_spec,
        ) = mock_git_fetcher.call_args.args
        assert call_parsed_url.original_url == expected_repo_url
        assert call_tmp_path == tmp_path
        assert call_git_ref == git_ref
        assert call_spec is None


@pytest.mark.parametrize(
    "url, workflow_name",
    [
        ("https://github.com/user/repo/archive/commit.zip", "repo-commit"),
        ("https://github.com/user/repo/archive/refs/heads/branch.zip", "repo-branch"),
        ("https://github.com/user/repo/archive/refs/tags/tag.zip", "repo-tag"),
    ],
)
def test_github_fetcher_zip(url, workflow_name, tmp_path):
    """Test creating a valid fetcher for GitHub URLs downloading zip snapshots."""
    mock_zip_fetcher = Mock()
    with patch("reana_server.fetcher.WorkflowFetcherZip", mock_zip_fetcher):
        _get_github_fetcher(ParsedUrl(url), tmp_path)
        mock_zip_fetcher.assert_called_once()
        (
            call_parsed_url,
            call_tmp_path,
            call_spec,
            call_workflow_name,
        ) = mock_zip_fetcher.call_args.args
        assert call_parsed_url.original_url == url
        assert call_tmp_path == tmp_path
        assert call_spec is None
        assert call_workflow_name == workflow_name


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/user/repo/invalid",
        "https://github.com/user/repo/blob/branch/path/to/file.txt",
        "https://github.com/user/repo/blob/branch/path/to/reana.yaml",
        "https://github.com/user",
        "https://github.com/",
    ],
)
@pytest.mark.xfail(raises=ValueError, strict=True)
def test_invalid_github_fetcher(url, tmp_path):
    """Test handling of invalid GitHub URLs."""
    _get_github_fetcher(ParsedUrl(url), tmp_path)


@pytest.mark.parametrize(
    "url, username, repository, git_ref",
    [
        ("https://gitlab.com/user/repo", "user", "repo", None),
        ("https://gitlab.cern.ch/user/repo", "user", "repo", None),
        ("https://gitlab.com/group/user/repo", "group/user", "repo", None),
        ("https://gitlab.com/user/repo.git/", "user", "repo", None),
        ("https://gitlab.com/group/user/repo.git/", "group/user", "repo", None),
        ("https://gitlab.com/user/repo/-/tree/branch", "user", "repo", "branch"),
        (
            "https://gitlab.com/group/user/repo/-/tree/branch",
            "group/user",
            "repo",
            "branch",
        ),
        (
            "https://gitlab.com/group/user/repo/-/tree/tag/with/slashes",
            "group/user",
            "repo",
            "tag/with/slashes",
        ),
        (
            "https://gitlab.com/group/user/repo/-/tree/tag/with/slashes/",
            "group/user",
            "repo",
            "tag/with/slashes",
        ),
    ],
)
def test_gitlab_fetcher(url, username, repository, git_ref, tmp_path):
    """Test creating a valid fetcher for GitLab URLs."""
    mock_git_fetcher = Mock()
    with patch("reana_server.fetcher.WorkflowFetcherGit", mock_git_fetcher):
        parsed_url = ParsedUrl(url)
        _get_gitlab_fetcher(ParsedUrl(url), tmp_path)
        mock_git_fetcher.assert_called_once()
        expected_repo_url = f"https://{parsed_url.hostname}/{username}/{repository}.git"
        (
            call_parsed_url,
            call_tmp_path,
            call_git_ref,
            call_spec,
        ) = mock_git_fetcher.call_args.args
        assert call_parsed_url.original_url == expected_repo_url
        assert call_tmp_path == tmp_path
        assert call_git_ref == git_ref
        assert call_spec is None


@pytest.mark.parametrize(
    "url, expected_name",
    [
        (GIT_URL, "reana-demo-root6-roofit"),
        (GIT_URL + "/", "reana-demo-root6-roofit"),
        (GITHUB_REPO_URL, "reana-demo-root6-roofit"),
        (GITHUB_REPO_URL + "/", "reana-demo-root6-roofit"),
        (GITHUB_REPO_URL + "/tree/branch", "reana-demo-root6-roofit-branch"),
        (
            GITHUB_REPO_URL + "/tree/tag/with/slashes/",
            "reana-demo-root6-roofit-tag-with-slashes",
        ),
        (GITHUB_REPO_ZIP, "reana-demo-root6-roofit-master"),
        (GITLAB_REPO_URL, "repo"),
        (GITLAB_REPO_URL + "/-/tree/tag/with/slashes/", "repo-tag-with-slashes"),
        (GITLAB_REPO_ZIP, "repo-master"),
        (ZENODO_URL, "circular-health-data-processing-master"),
        (YAML_URL, "reanahub-reana-demo-root6-roofit-master"),
        ("https://example.org/reana-snakemake.yaml", "reana-snakemake"),
    ],
)
def test_workflow_name_generation(url, expected_name, tmp_path):
    """Test the generation of the workflow name from the given URL."""
    assert get_fetcher(url, tmp_path).generate_workflow_name() == expected_name


@patch("reana_server.fetcher.FETCHER_MAXIMUM_FILE_SIZE", 100)
def test_size_limit(tmp_path):
    """Test the maximum file size of the file to be downloaded."""
    mock_request = Mock()
    mock_request.headers = {"Content-Length": 101}
    mock_request_context_manager = MagicMock()
    mock_request_context_manager.__enter__.return_value = mock_request
    mock_requests = Mock()
    mock_requests.get.return_value = mock_request_context_manager

    with patch("reana_server.fetcher.requests", mock_requests):
        with pytest.raises(REANAFetcherError, match="file size exceeded"):
            WorkflowFetcherBase._download_file(YAML_URL, tmp_path)


@patch("reana_server.fetcher.FETCHER_MAXIMUM_FILE_SIZE", 100)
def test_size_limit_without_content_length(tmp_path):
    """Test the maximum file size of the file to be downloaded when ``Content-Length``
    is not provided.
    """
    mock_request = Mock()
    mock_request.headers = {}
    mock_request.iter_content.return_value = [b"a" * 101]
    mock_request_context_manager = MagicMock()
    mock_request_context_manager.__enter__.return_value = mock_request
    mock_requests = Mock()
    mock_requests.get.return_value = mock_request_context_manager

    file_path = os.path.join(tmp_path, "file")
    with patch("reana_server.fetcher.requests", mock_requests):
        with pytest.raises(REANAFetcherError, match="file size exceeded"):
            WorkflowFetcherBase._download_file(YAML_URL, file_path)
    assert not os.path.exists(file_path)
