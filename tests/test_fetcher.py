# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022, 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server workflow fetcher tests."""

import os
import subprocess
import sys
from urllib.request import urlretrieve
import pytest
from unittest.mock import MagicMock, Mock, patch
import zipfile
import struct

from git import Repo

from reana_server.fetcher import (
    _get_github_fetcher,
    _get_gitlab_fetcher,
    _remote_ref_candidates,
    _remote_refs,
    _resolve_provider_tree_path,
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


def create_git_repository(repo_path, files):
    """Create a git repository with one commit for each file."""
    repository = Repo.init(repo_path, initial_branch="main")

    commits = []
    for file, content in files:
        file_path = os.path.join(repo_path, file)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w") as f:
            f.write(content)
        repository.index.add(file_path)
        commit = repository.index.commit(f"Add {file}")
        commits.append(commit.hexsha)

    return repository, commits


def create_git_repository_with_refs(repo_path):
    """Create a local repository advertising branches, tags and a decoy ref."""
    repository, _ = create_git_repository(repo_path, [("reana.yaml", "spec\n")])
    with repository.config_writer() as config:
        config.set_value("user", "name", "REANA")
        config.set_value("user", "email", "info@reana.io")
    repository.create_head("feature/x")
    # Annotated tags also advertise a dereferenced ``^{}`` line.
    repository.create_tag("v1.0", message="Release 1.0")
    # ``ls-remote`` tail-matches literal operands, so this ref is also returned
    # when querying ``refs/heads/main`` and must be discarded by the exact filter.
    repository.create_head("decoy/refs/heads/main")
    return repository


def create_zip_file(archive_path, files):
    """Create a zip archive with the given files."""
    with zipfile.ZipFile(archive_path, "w") as zip_file:
        for file, content in files:
            zip_file.writestr(file, content)


def download_from_archive(archive_path):
    """Return a download mock side effect that copies a local archive."""

    def download(_url, output_path):
        urlretrieve(f"file://{archive_path}", output_path)

    return download


@pytest.mark.parametrize(
    "url, expected_fetcher_class",
    [
        (GIT_URL, WorkflowFetcherZip),
        (GIT_URL + "/", WorkflowFetcherZip),
        (GITHUB_REPO_URL, WorkflowFetcherZip),
        (GITHUB_REPO_URL + "/", WorkflowFetcherZip),
        (GITHUB_REPO_ZIP, WorkflowFetcherZip),
        (GITLAB_REPO_URL, WorkflowFetcherZip),
        (GITLAB_REPO_URL + "/", WorkflowFetcherZip),
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
    repo_dir = os.path.join(tmp_path, "repo")
    output_dir = os.path.join(tmp_path, "output")

    files = [
        ("reana.yaml", "Content of reana.yaml"),
        ("reana-cwl.yaml", "Content of reana-cwl.yaml"),
        ("README.md", "# Test Git Repository"),
        ("reana-not-present.yaml", "Content of reana-not-present.yaml"),
    ]

    repository, commits = create_git_repository(repo_dir, files)
    repository.git.checkout(commits[1])
    repository.create_head("new-branch")
    repository.create_tag("new-tag")
    repository.git.checkout("main")

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


def test_fetcher_git_enforces_temporary_clone_limit(tmp_path):
    """The generic Git fallback is killed before its clone tree grows unbounded."""
    repository_path = tmp_path / "repository"
    repository = Repo.init(repository_path, initial_branch="main")
    (repository_path / "reana.yaml").write_text("x" * 4096)
    repository.index.add("reana.yaml")
    repository.index.commit("Add specification")

    fetcher = WorkflowFetcherGit(
        ParsedUrl(f"file://{repository_path}"), str(tmp_path / "output")
    )
    with patch("reana_server.fetcher.FETCHER_MAXIMUM_CLONE_SIZE", 128):
        with pytest.raises(REANAFetcherError, match="Cannot clone|storage limit"):
            fetcher.fetch()


def test_fetcher_git_timeout_kills_and_reaps_process(tmp_path):
    """A stalled generic Git process is terminated at the shared deadline."""

    class StalledProcess:
        def __init__(self):
            self.pid = 123
            self.returncode = None
            self.killed = False
            self.wait_timeouts = []

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("git", timeout)
            return self.returncode

    process = StalledProcess()
    fetcher = WorkflowFetcherGit(
        ParsedUrl("https://example.org/repository.git"),
        str(tmp_path / "output"),
    )
    fetcher._clone_tree_exceeds_limits = Mock(return_value=False)

    with patch("reana_server.fetcher.subprocess.Popen", return_value=process), patch(
        "reana_server.fetcher.resource.prlimit", create=True
    ), patch("reana_server.fetcher.FETCHER_REQUEST_TIMEOUT", 1), patch(
        "reana_server.fetcher.time.monotonic", side_effect=[0, 0, 2]
    ):
        with pytest.raises(REANAFetcherError, match="timed out"):
            fetcher._run_bounded_git(["git", "clone"])

    assert process.killed
    assert process.wait_timeouts == [0.5, None]
    assert fetcher._clone_tree_exceeds_limits.call_count == 2


def test_fetcher_git_fast_completion_keeps_final_strict_scan(tmp_path):
    """A completed clone is reaped promptly and receives the strict final scan."""

    class CompletingProcess:
        def __init__(self):
            self.pid = 123
            self.returncode = None
            self.wait_timeouts = []

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            self.returncode = 0
            return self.returncode

        def kill(self):
            raise AssertionError("successful process must not be killed")

    process = CompletingProcess()
    fetcher = WorkflowFetcherGit(
        ParsedUrl("https://example.org/repository.git"),
        str(tmp_path / "output"),
    )
    fetcher._clone_tree_exceeds_limits = Mock(side_effect=[False, False])

    with patch("reana_server.fetcher.subprocess.Popen", return_value=process), patch(
        "reana_server.fetcher.resource.prlimit", create=True
    ), patch("reana_server.fetcher.time.monotonic", side_effect=[0, 0]):
        assert fetcher._run_bounded_git(["git", "clone"])

    assert process.wait_timeouts == [0.5]
    assert fetcher._clone_tree_exceeds_limits.call_args_list[1].kwargs["strict"]


@pytest.mark.parametrize(
    "url, remote_refs, expected_archive_ref, expected_workflow_name",
    [
        (
            "https://github.com/user/repo/tree/main/workflows/example",
            {"refs/heads/main"},
            "/archive/main.zip",
            "repo-main-workflows-example",
        ),
        (
            "https://gitlab.com/group/user/repo/-/tree/release/v1/workflows/example",
            {"refs/heads/release", "refs/tags/release/v1"},
            "sha=release%2Fv1",
            "repo-release-v1-workflows-example",
        ),
        (
            "https://github.com/user/repo/tree/HEAD/workflows/example",
            set(),
            "/archive/HEAD.zip",
            "repo-HEAD-workflows-example",
        ),
        (
            "https://gitlab.com/group/user/repo/-/tree/HEAD/foo/workflows/example",
            {"refs/tags/HEAD/foo"},
            "sha=HEAD%2Ffoo",
            "repo-HEAD-foo-workflows-example",
        ),
    ],
)
def test_provider_fetcher_tree_path_prefers_longest_ref_prefix(
    url, remote_refs, expected_archive_ref, expected_workflow_name, tmp_path
):
    """Test provider tree URLs prefer the longest ref and select workflow roots."""
    archive_path = os.path.join(tmp_path, "archive.zip")
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)
    create_zip_file(
        archive_path,
        [
            ("repo-main/reana.yaml", "root spec\n"),
            ("repo-main/workflows/example/reana.yaml", "nested spec\n"),
            ("repo-main/workflows/example/helloworld.py", "print('hello')\n"),
        ],
    )

    with patch("reana_server.fetcher._remote_refs", return_value=remote_refs), patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file",
        side_effect=download_from_archive(archive_path),
    ) as mock_download:
        fetcher = get_fetcher(url, output_dir)
        assert isinstance(fetcher, WorkflowFetcherZip)
        assert fetcher.generate_workflow_name() == expected_workflow_name
        fetcher.fetch()

    assert expected_archive_ref in mock_download.call_args.args[0]
    expected_root_path = os.path.join(output_dir, "workflows", "example")
    assert fetcher.workflow_root_path() == expected_root_path
    assert fetcher.workflow_spec_path() == os.path.join(
        expected_root_path, "reana.yaml"
    )


@pytest.mark.parametrize(
    "url, workflow_path, expected_workflow_name",
    [
        (
            "https://github.com/user/repo/tree/main/workflows/my%20analysis",
            "workflows/my analysis",
            "repo-main-workflows-my-analysis",
        ),
        (
            "https://gitlab.com/user/repo/-/tree/main/workflows/caf%C3%A9",
            "workflows/café",
            "repo-main-workflows-caf",
        ),
    ],
)
def test_provider_fetcher_decodes_percent_encoded_tree_paths(
    url, workflow_path, expected_workflow_name, tmp_path
):
    """Test provider tree URL path segments are decoded exactly once."""
    archive_path = os.path.join(tmp_path, "archive.zip")
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)
    create_zip_file(
        archive_path,
        [
            ("repo-main/reana.yaml", "root spec\n"),
            (f"repo-main/{workflow_path}/reana.yaml", "nested spec\n"),
        ],
    )

    with patch(
        "reana_server.fetcher._remote_refs", return_value={"refs/heads/main"}
    ), patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file",
        side_effect=download_from_archive(archive_path),
    ):
        fetcher = get_fetcher(url, output_dir)
        assert fetcher.generate_workflow_name() == expected_workflow_name
        fetcher.fetch()

    expected_root_path = os.path.join(output_dir, *workflow_path.split("/"))
    assert fetcher.workflow_root_path() == expected_root_path
    assert fetcher.workflow_spec_path() == os.path.join(
        expected_root_path, "reana.yaml"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/user/repo/tree/main/%2E%2E/etc",
        "https://github.com/user/repo/tree/main/workflows%2Fexample",
        "https://github.com/user/repo/tree/main/%00",
        "https://github.com/user/repo/tree/main//workflows/example",
        "https://gitlab.com/user/repo/-/tree/main/%5C",
        "https://gitlab.com/user/repo/-/tree/main/%ZZ",
    ],
)
def test_provider_fetcher_rejects_invalid_tree_path_segments(url, tmp_path):
    """Test unsafe provider tree URL segments are rejected before ref lookup."""
    with patch(
        "reana_server.fetcher._remote_refs",
        side_effect=AssertionError("remote refs must not be queried"),
    ):
        with pytest.raises(
            REANAFetcherError, match="Invalid path to the workflow directory"
        ):
            get_fetcher(url, tmp_path)


def test_provider_fetcher_does_not_expand_encoded_git_globs(tmp_path):
    """Test encoded Git glob characters cannot broaden remote ref discovery."""
    with patch("reana_server.fetcher.subprocess.Popen") as popen:
        with pytest.raises(
            REANAFetcherError,
            match=r'Cannot checkout the given Git reference "\*/workflow"',
        ):
            get_fetcher(
                "https://github.com/user/repo/tree/%2A/workflow",
                tmp_path,
            )

    popen.assert_not_called()


def test_provider_fetcher_full_sha_folder_works_when_remote_refs_fail(tmp_path):
    """Test full commit-SHA tree URLs can fall back when ref discovery fails."""
    git_ref = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    archive_path = os.path.join(tmp_path, "archive.zip")
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)
    create_zip_file(
        archive_path,
        [("repo-main/workflows/example/reana.yaml", "nested spec\n")],
    )

    with patch(
        "reana_server.fetcher._remote_refs",
        side_effect=REANAFetcherError("Cannot resolve refs"),
    ), patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file",
        side_effect=download_from_archive(archive_path),
    ) as mock_download:
        fetcher = get_fetcher(
            f"https://github.com/user/repo/tree/{git_ref}/workflows/example",
            output_dir,
        )
        fetcher.fetch()

    assert f"/archive/{git_ref}.zip" in mock_download.call_args.args[0]
    expected_root_path = os.path.join(output_dir, "workflows", "example")
    assert fetcher.workflow_spec_path() == os.path.join(
        expected_root_path, "reana.yaml"
    )


def test_provider_fetcher_head_folder_works_when_remote_refs_fail(tmp_path):
    """Test symbolic HEAD tree URLs remain available when ref discovery fails."""
    archive_path = os.path.join(tmp_path, "archive.zip")
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)
    create_zip_file(
        archive_path,
        [("repo-main/workflows/example/reana.yaml", "nested spec\n")],
    )

    with patch(
        "reana_server.fetcher._remote_refs",
        side_effect=REANAFetcherError("Cannot resolve refs"),
    ), patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file",
        side_effect=download_from_archive(archive_path),
    ) as mock_download:
        fetcher = get_fetcher(
            "https://github.com/user/repo/tree/HEAD/workflows/example",
            output_dir,
        )
        fetcher.fetch()

    assert "/archive/HEAD.zip" in mock_download.call_args.args[0]
    expected_root_path = os.path.join(output_dir, "workflows", "example")
    assert fetcher.workflow_spec_path() == os.path.join(
        expected_root_path, "reana.yaml"
    )


def test_provider_fetcher_abbreviated_sha_folder_requires_remote_ref_lookup(tmp_path):
    """Test ambiguous abbreviated SHAs still require successful ref discovery."""
    with patch(
        "reana_server.fetcher._remote_refs",
        side_effect=REANAFetcherError("Cannot resolve refs"),
    ):
        with pytest.raises(REANAFetcherError, match="Cannot resolve refs"):
            get_fetcher(
                "https://github.com/user/repo/tree/abcdef0/workflows/example",
                tmp_path,
            )


def test_provider_fetcher_tree_path_with_explicit_spec_in_workflow_root(tmp_path):
    """Test explicit specification paths resolve inside selected workflow roots."""
    archive_path = os.path.join(tmp_path, "archive.zip")
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)
    create_zip_file(
        archive_path,
        [
            ("repo-main/reana.yaml", "root spec\n"),
            ("repo-main/nested/custom.yaml", "nested spec\n"),
        ],
    )

    with patch(
        "reana_server.fetcher._remote_refs", return_value={"refs/heads/main"}
    ), patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file",
        side_effect=download_from_archive(archive_path),
    ):
        fetcher = get_fetcher(
            "https://github.com/user/repo/tree/main/nested",
            output_dir,
            spec="custom.yaml",
        )
        fetcher.fetch()

    assert fetcher.workflow_spec_path() == os.path.join(
        output_dir, "nested", "custom.yaml"
    )


def test_provider_fetcher_tree_path_does_not_fallback_to_repo_root_spec(tmp_path):
    """Test folder URLs do not resolve explicit specs outside the workflow root."""
    archive_path = os.path.join(tmp_path, "archive.zip")
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)
    create_zip_file(
        archive_path,
        [
            ("repo-main/reana.yaml", "root spec\n"),
            ("repo-main/nested/README.md", "nested placeholder\n"),
        ],
    )

    with patch(
        "reana_server.fetcher._remote_refs", return_value={"refs/heads/main"}
    ), patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file",
        side_effect=download_from_archive(archive_path),
    ):
        fetcher = get_fetcher(
            "https://github.com/user/repo/tree/main/nested",
            output_dir,
            spec="reana.yaml",
        )
        fetcher.fetch()

    with pytest.raises(
        REANAFetcherError, match="Cannot find the provided workflow specification"
    ):
        fetcher.workflow_spec_path()


@pytest.mark.parametrize("spec", ["../reana.yaml", "../../etc/passwd.yaml"])
def test_provider_fetcher_rejects_spec_path_traversal(spec, tmp_path):
    """Test explicit specs cannot escape the selected workflow root."""
    archive_path = os.path.join(tmp_path, "archive.zip")
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)
    create_zip_file(
        archive_path,
        [
            ("repo-main/reana.yaml", "root spec\n"),
            ("repo-main/nested/reana.yaml", "nested spec\n"),
        ],
    )

    with patch(
        "reana_server.fetcher._remote_refs", return_value={"refs/heads/main"}
    ), patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file",
        side_effect=download_from_archive(archive_path),
    ):
        fetcher = get_fetcher(
            "https://github.com/user/repo/tree/main/nested",
            output_dir,
            spec=spec,
        )
        fetcher.fetch()

    with pytest.raises(
        REANAFetcherError, match="Invalid path to the workflow specification"
    ):
        fetcher.workflow_spec_path()


def test_remote_refs_queries_only_candidate_refs():
    """Test remote ref discovery asks Git only for possible tree refs."""
    output = (
        b"abc123\trefs/heads/main\n"
        b"abc123\trefs/tags/main\n"
        b"abc123\trefs/heads/other\n"
    )
    ref_candidates = ("refs/heads/main", "refs/tags/main")
    with patch(
        "reana_server.fetcher._run_bounded_git_output", return_value=output
    ) as run:
        assert _remote_refs("https://example.org/repo.git", ref_candidates) == {
            "refs/heads/main",
            "refs/tags/main",
        }

    assert run.call_args.args[0] == [
        "git",
        "ls-remote",
        "--heads",
        "--tags",
        "--",
        "https://example.org/repo.git",
        "refs/heads/main",
        "refs/tags/main",
    ]
    assert run.call_args.args[1] > 0
    assert run.call_args.args[2] == 4


def test_remote_ref_candidates_exclude_git_glob_patterns():
    """Test user-controlled glob characters cannot broaden remote ref output."""
    assert _remote_ref_candidates(["main", "*"]) == (
        "refs/heads/main",
        "refs/tags/main",
    )
    assert _remote_ref_candidates(["*", "workflow"]) == ()
    assert _remote_ref_candidates(["HEAD", "foo"]) == (
        "refs/heads/HEAD/foo",
        "refs/tags/HEAD/foo",
    )


def test_remote_refs_does_not_query_glob_candidates():
    """Test remote ref discovery skips a set containing only glob patterns."""
    with patch("reana_server.fetcher._run_bounded_git_output") as run:
        assert _remote_refs("https://example.org/repo.git", ["refs/heads/*"]) == set()

    run.assert_not_called()


def test_remote_refs_failure_raises_fetcher_error():
    """Test remote ref resolution failures are reported as fetcher errors."""
    with patch("reana_server.fetcher.subprocess.Popen", side_effect=OSError):
        with pytest.raises(
            REANAFetcherError, match="Cannot resolve Git references from the given"
        ):
            _remote_refs("https://example.org/repo.git", ["refs/heads/main"])


def test_remote_refs_rejects_excessive_suffix_matches(tmp_path):
    """Test suffix-matching refs cannot produce unbounded buffered output."""
    fake_git = tmp_path / "git"
    fake_git.write_text(
        f"#!{sys.executable}\n"
        "for index in range(100):\n"
        "    print('0' * 40 + '\\trefs/heads/prefix-' + "
        "str(index) + '/refs/heads/main')\n"
    )
    fake_git.chmod(0o755)

    with patch.dict(os.environ, {"PATH": str(tmp_path)}):
        with pytest.raises(
            REANAFetcherError,
            match="Git reference discovery output exceeded its limit",
        ):
            _remote_refs("https://example.org/repo.git", ["refs/heads/main"])


def test_remote_refs_reaps_git_on_unexpected_errors(tmp_path):
    """Test the Git child is reaped on exit paths other than expected errors."""
    fake_git = tmp_path / "git"
    fake_git.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    fake_git.chmod(0o755)

    processes = []
    original_popen = subprocess.Popen

    def record_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    with patch.dict(os.environ, {"PATH": str(tmp_path)}), patch(
        "reana_server.fetcher.subprocess.Popen", side_effect=record_popen
    ), patch(
        "reana_server.fetcher.selectors.DefaultSelector",
        side_effect=RuntimeError("Unexpected selector failure"),
    ):
        with pytest.raises(RuntimeError, match="Unexpected selector failure"):
            _remote_refs("https://example.org/repo.git", ["refs/heads/main"])

    assert len(processes) == 1
    assert processes[0].poll() is not None


def test_remote_refs_matches_real_repository_refs(tmp_path):
    """Test remote ref discovery parses real bounded ``git ls-remote`` output."""
    repo_path = os.path.join(tmp_path, "repo")
    create_git_repository_with_refs(repo_path)

    assert _remote_refs(
        repo_path,
        [
            "refs/heads/feature/x",
            "refs/tags/feature/x",
            "refs/heads/main",
            "refs/tags/main",
            "refs/heads/v1.0",
            "refs/tags/v1.0",
        ],
    ) == {"refs/heads/feature/x", "refs/heads/main", "refs/tags/v1.0"}


def test_remote_refs_missing_repository_raises_fetcher_error(tmp_path):
    """Test a non-zero Git exit is reported as a fetcher error."""
    missing_repo_path = os.path.join(tmp_path, "missing")

    with pytest.raises(
        REANAFetcherError, match="Cannot resolve Git references from the given"
    ):
        _remote_refs(missing_repo_path, ["refs/heads/main"])


def test_resolve_provider_tree_path_uses_real_remote_refs(tmp_path):
    """Test tree path resolution against real remote ref discovery."""
    repo_path = os.path.join(tmp_path, "repo")
    create_git_repository_with_refs(repo_path)

    assert _resolve_provider_tree_path(repo_path, "feature/x/workflows/example") == (
        "feature/x",
        "workflows/example",
        "feature/x/workflows/example",
    )
    assert _resolve_provider_tree_path(repo_path, "v1.0/workflows/example") == (
        "v1.0",
        "workflows/example",
        "v1.0/workflows/example",
    )
    # Abbreviated SHAs are accepted once discovery proves no ref takes precedence.
    assert _resolve_provider_tree_path(repo_path, "abcdef0/workflows/example") == (
        "abcdef0",
        "workflows/example",
        "abcdef0/workflows/example",
    )
    with pytest.raises(REANAFetcherError, match="Cannot checkout the given Git"):
        _resolve_provider_tree_path(repo_path, "unknown/workflows/example")


def test_provider_fetcher_abbreviated_sha_folder_after_successful_discovery(tmp_path):
    """Test abbreviated SHAs are used once discovery finds no matching ref."""
    archive_path = os.path.join(tmp_path, "archive.zip")
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)
    create_zip_file(
        archive_path,
        [("repo-main/workflows/example/reana.yaml", "nested spec\n")],
    )

    with patch(
        "reana_server.fetcher._remote_refs", return_value={"refs/heads/main"}
    ), patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file",
        side_effect=download_from_archive(archive_path),
    ) as mock_download:
        fetcher = get_fetcher(
            "https://github.com/user/repo/tree/abcdef0/workflows/example",
            output_dir,
        )
        fetcher.fetch()

    assert "/archive/abcdef0.zip" in mock_download.call_args.args[0]
    expected_root_path = os.path.join(output_dir, "workflows", "example")
    assert fetcher.workflow_spec_path() == os.path.join(
        expected_root_path, "reana.yaml"
    )


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


def test_fetcher_zip_with_workflow_directory(tmp_path):
    """Test archive fetchers can select a workflow directory after extraction."""
    input_dir = os.path.join(tmp_path, "input")
    os.makedirs(input_dir)
    output_dir = os.path.join(tmp_path, "output")
    os.makedirs(output_dir)
    archive_path = os.path.join(input_dir, "archive.zip")
    with zipfile.ZipFile(archive_path, "w") as zip_file:
        zip_file.writestr("repo-main/reana.yaml", "root spec\n")
        zip_file.writestr("repo-main/workflows/example/reana.yaml", "nested spec\n")

    mock_download = Mock()
    mock_download.side_effect = urlretrieve
    with patch(
        "reana_server.fetcher.WorkflowFetcherBase._download_file", mock_download
    ):
        fetcher = WorkflowFetcherZip(
            ParsedUrl(f"file://{archive_path}"),
            output_dir,
            workflow_path="workflows/example",
        )
        fetcher.fetch()

    expected_root_path = os.path.join(output_dir, "workflows", "example")
    assert fetcher.workflow_root_path() == expected_root_path
    assert fetcher.workflow_spec_path() == os.path.join(
        expected_root_path, "reana.yaml"
    )


def test_fetcher_zip_counts_directory_entries_towards_limit():
    """An archive cannot bypass its entry cap using empty directories."""
    entries = [zipfile.ZipInfo("one/"), zipfile.ZipInfo("two/")]
    with patch("reana_server.fetcher.FETCHER_MAXIMUM_FILES", 1):
        with pytest.raises(REANAFetcherError, match="too many archive entries"):
            WorkflowFetcherZip._validate_archive_entries(entries)


def test_fetcher_zip_bounds_depth_and_implicit_directories():
    """Remote archive metadata cannot create an unbounded directory tree."""
    with patch("reana_server.fetcher.SPECIFICATION_BUNDLE_MAX_DEPTH", 2):
        with pytest.raises(REANAFetcherError, match="metadata limits"):
            WorkflowFetcherZip._validate_archive_entries(
                [zipfile.ZipInfo("one/two/file")]
            )

    with patch("reana_server.fetcher._FETCHER_MAXIMUM_DIRECTORIES", 1):
        with pytest.raises(REANAFetcherError, match="too many directories"):
            WorkflowFetcherZip._validate_archive_entries(
                [zipfile.ZipInfo("one/file"), zipfile.ZipInfo("two/file")]
            )


def test_fetcher_git_scan_bounds_directory_breadth(tmp_path):
    """A clone with few files but excessive directories is rejected."""
    output = tmp_path / "output"
    output.mkdir()
    (output / "one").mkdir()
    (output / "two").mkdir()
    fetcher = WorkflowFetcherGit(
        ParsedUrl("https://example.org/repository.git"), str(output)
    )

    with patch("reana_server.fetcher._FETCHER_MAXIMUM_DIRECTORIES", 1):
        assert fetcher._clone_tree_exceeds_limits(file_limit=10, strict=True)


@pytest.mark.parametrize(
    "names",
    [
        ["a", "a/b"],
        ["a/b", "a"],
        ["a", "a/"],
        ["a/", "a"],
    ],
)
def test_fetcher_zip_rejects_file_ancestor_collision(names):
    """Remote archives cannot represent a file as a directory or ancestor."""
    entries = [zipfile.ZipInfo(name) for name in names]
    with pytest.raises(REANAFetcherError):
        WorkflowFetcherZip._validate_archive_entries(entries)


def test_fetcher_zip_converts_extraction_oserror(monkeypatch, tmp_path):
    """Filesystem extraction failures use the controlled fetcher error."""
    archive_path = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("reana.yaml", "workflow: {}")
    output_path = tmp_path / "output"
    output_path.mkdir()
    fetcher = WorkflowFetcherZip(
        ParsedUrl("file:///archive.zip"),
        str(output_path),
    )

    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()

        def fail_makedirs(*args, **kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr("reana_server.fetcher.os.makedirs", fail_makedirs)
        with pytest.raises(REANAFetcherError):
            fetcher._extract_archive_entries(archive, entries)


def test_fetcher_rejects_entry_count_before_zipfile(monkeypatch, tmp_path):
    """Remote archive metadata is bounded before entry materialisation."""
    archive_path = tmp_path / "archive.zip"
    archive_path.write_bytes(
        struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 2, 2, 0, 0, 0)
    )
    output_path = tmp_path / "output"
    output_path.mkdir()
    fetcher = WorkflowFetcherZip(ParsedUrl("file:///archive.zip"), str(output_path))
    monkeypatch.setattr("reana_server.fetcher.FETCHER_MAXIMUM_FILES", 1)
    zip_file = Mock(side_effect=AssertionError("ZipFile must not be constructed"))
    monkeypatch.setattr("reana_server.fetcher.zipfile.ZipFile", zip_file)

    with pytest.raises(REANAFetcherError, match="too many entries"):
        fetcher.extract_archive(str(archive_path))

    zip_file.assert_not_called()


@pytest.mark.parametrize(
    "url, username, repository, tree_path, remote_refs, expected_ref, workflow_path",
    [
        ("https://github.com/user/repo", "user", "repo", None, set(), None, None),
        ("https://github.com/user/repo/", "user", "repo", None, set(), None, None),
        ("https://github.com/user/repo.git", "user", "repo", None, set(), None, None),
        ("https://github.com/user/repo.git/", "user", "repo", None, set(), None, None),
        (
            "https://github.com/user/repo/tree/branch",
            "user",
            "repo",
            "branch",
            set(),
            "branch",
            None,
        ),
        (
            "https://github.com/user/repo/tree/branch/",
            "user",
            "repo",
            "branch",
            set(),
            "branch",
            None,
        ),
        (
            "https://github.com/user/repo/tree/tag/with/slashes",
            "user",
            "repo",
            "tag/with/slashes",
            {"refs/heads/tag/with/slashes"},
            "tag/with/slashes",
            None,
        ),
        (
            "https://github.com/user/repo/tree/tag/with/slashes/",
            "user",
            "repo",
            "tag/with/slashes",
            {"refs/heads/tag/with/slashes"},
            "tag/with/slashes",
            None,
        ),
        (
            "https://github.com/user/repo/tree/branch/workflows/example",
            "user",
            "repo",
            "branch/workflows/example",
            {"refs/heads/branch"},
            "branch",
            "workflows/example",
        ),
        (
            "https://github.com/user/repo/tree/tag/with/slashes/workflows/example",
            "user",
            "repo",
            "tag/with/slashes/workflows/example",
            {"refs/tags/tag/with/slashes"},
            "tag/with/slashes",
            "workflows/example",
        ),
    ],
)
def test_github_fetcher(
    url,
    username,
    repository,
    tree_path,
    remote_refs,
    expected_ref,
    workflow_path,
    tmp_path,
):
    """GitHub repositories use bounded provider ZIP snapshots."""
    mock_zip_fetcher = Mock()
    with patch("reana_server.fetcher.WorkflowFetcherZip", mock_zip_fetcher), patch(
        "reana_server.fetcher._remote_refs",
        return_value=remote_refs,
    ):
        _get_github_fetcher(ParsedUrl(url), tmp_path)
        mock_zip_fetcher.assert_called_once()
        (
            call_parsed_url,
            call_tmp_path,
            call_spec,
            call_workflow_name,
            call_workflow_path,
        ) = mock_zip_fetcher.call_args.args
        assert call_parsed_url.original_url.startswith(
            f"https://github.com/{username}/{repository}/archive/"
        )
        assert call_parsed_url.original_url.endswith(".zip")
        assert (
            f"/archive/{expected_ref}.zip" in call_parsed_url.original_url
            if expected_ref
            else "/archive/HEAD.zip" in call_parsed_url.original_url
        )
        assert call_tmp_path == tmp_path
        assert call_spec is None
        assert call_workflow_name == (
            repository if not tree_path else f"{repository}-{tree_path}"
        )
        assert call_workflow_path == workflow_path


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
    "url, username, repository, tree_path, remote_refs, expected_ref, workflow_path",
    [
        ("https://gitlab.com/user/repo", "user", "repo", None, set(), None, None),
        ("https://gitlab.cern.ch/user/repo", "user", "repo", None, set(), None, None),
        (
            "https://gitlab.com/group/user/repo",
            "group/user",
            "repo",
            None,
            set(),
            None,
            None,
        ),
        ("https://gitlab.com/user/repo.git/", "user", "repo", None, set(), None, None),
        (
            "https://gitlab.com/group/user/repo.git/",
            "group/user",
            "repo",
            None,
            set(),
            None,
            None,
        ),
        (
            "https://gitlab.com/user/repo/-/tree/branch",
            "user",
            "repo",
            "branch",
            set(),
            "branch",
            None,
        ),
        (
            "https://gitlab.com/group/user/repo/-/tree/branch",
            "group/user",
            "repo",
            "branch",
            set(),
            "branch",
            None,
        ),
        (
            "https://gitlab.com/group/user/repo/-/tree/tag/with/slashes",
            "group/user",
            "repo",
            "tag/with/slashes",
            {"refs/heads/tag/with/slashes"},
            "tag/with/slashes",
            None,
        ),
        (
            "https://gitlab.com/group/user/repo/-/tree/tag/with/slashes/",
            "group/user",
            "repo",
            "tag/with/slashes",
            {"refs/heads/tag/with/slashes"},
            "tag/with/slashes",
            None,
        ),
        (
            "https://gitlab.com/group/user/repo/-/tree/branch/workflows/example",
            "group/user",
            "repo",
            "branch/workflows/example",
            {"refs/heads/branch"},
            "branch",
            "workflows/example",
        ),
        (
            "https://gitlab.com/group/user/repo/-/tree/tag/with/slashes/workflows/example",
            "group/user",
            "repo",
            "tag/with/slashes/workflows/example",
            {"refs/tags/tag/with/slashes"},
            "tag/with/slashes",
            "workflows/example",
        ),
    ],
)
def test_gitlab_fetcher(
    url,
    username,
    repository,
    tree_path,
    remote_refs,
    expected_ref,
    workflow_path,
    tmp_path,
):
    """GitLab repositories use bounded provider ZIP snapshots."""
    mock_zip_fetcher = Mock()
    with patch("reana_server.fetcher.WorkflowFetcherZip", mock_zip_fetcher), patch(
        "reana_server.fetcher._remote_refs",
        return_value=remote_refs,
    ):
        parsed_url = ParsedUrl(url)
        _get_gitlab_fetcher(ParsedUrl(url), tmp_path)
        mock_zip_fetcher.assert_called_once()
        (
            call_parsed_url,
            call_tmp_path,
            call_spec,
            call_workflow_name,
            call_workflow_path,
        ) = mock_zip_fetcher.call_args.args
        assert call_parsed_url.original_url.startswith(
            f"https://{parsed_url.hostname}/api/v4/projects/"
        )
        assert "repository/archive.zip?sha=" in call_parsed_url.original_url
        assert (
            f"sha={expected_ref.replace('/', '%2F')}" in call_parsed_url.original_url
            if expected_ref
            else "sha=HEAD" in call_parsed_url.original_url
        )
        assert call_tmp_path == tmp_path
        assert call_spec is None
        assert call_workflow_name == (
            repository if not tree_path else f"{repository}-{tree_path}"
        )
        assert call_workflow_path == workflow_path


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
    with patch(
        "reana_server.fetcher._remote_refs",
        return_value={"refs/heads/tag/with/slashes"},
    ):
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
