# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2023 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server deleter tests."""
import pathlib
import uuid

import pytest
from reana_commons.workspace import iterdir, walk
from reana_server.deleter import Deleter, InOrOut


@pytest.mark.parametrize(
    "initial_list, which_to_keep, final_list",
    [
        (
            [
                "inputs/input1.txt",
                "outputs/output1.txt",
                "temp.txt",
                "inputs/input2.txt",
            ],
            InOrOut.INPUTS_OUTPUTS,
            ["inputs/input1.txt", "outputs/output1.txt", "inputs/input2.txt"],
        ),
        (
            [
                "inputs/input1.txt",
                "outputs/output1.txt",
                "input.txt",
                "output.txt" "temp.txt",
                "inputs/input2.txt",
            ],
            InOrOut.INPUTS,
            ["inputs/input1.txt", "inputs/input2.txt", "input.txt"],
        ),
        (
            [
                "inputs/input1.txt",
                "outputs/output1.txt",
                "temp.txt",
                "inputs/input2.txt",
            ],
            InOrOut.OUTPUTS,
            ["outputs/output1.txt"],
        ),
        (
            [
                "inputs/input1.txt",
                "outputs/output1.txt",
                "temp.txt",
                "inputs/input2.txt",
                "input.txt",
                "output.txt",
            ],
            InOrOut.NONE,
            [],
        ),
    ],
)
def test_file_deletion(
    initial_list, which_to_keep, final_list, user0, sample_serial_workflow_in_db
):
    """Test delete files preserving inputs/outputs/none"""

    def init_workspace(ws, files):
        for file in files:
            f = ws / file
            f.parent.mkdir(0o755, parents=True, exist_ok=True)
            f.touch()
            assert f.exists()

    workflow = sample_serial_workflow_in_db
    workflow.reana_specification = dict(workflow.reana_specification)
    workflow.reana_specification["inputs"] = {
        "files": ["input.txt"],
        "directories": ["inputs"],
    }
    workflow.reana_specification["outputs"] = {
        "files": ["output.txt"],
        "directories": ["outputs"],
    }
    workspace = pathlib.Path(workflow.workspace_path)
    init_workspace(workspace, initial_list)
    deleter = Deleter(workflow)
    for file_or_dir in iterdir(deleter.workspace, ""):
        deleter.delete_files(which_to_keep, file_or_dir)

    # Make sure that the elements are the same
    files_in_workspace = [f for f in walk(workspace, include_dirs=False)]
    assert set(files_in_workspace) == set(final_list)
