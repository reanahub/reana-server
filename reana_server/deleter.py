# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2023 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA utilities to delete file in a workspace."""

import errno
import pathlib
from enum import Enum
from pathlib import Path
from typing import Union

import click
from reana_commons import workspace
from reana_db.models import Workflow

from reana_server.utils import is_relative_to


class InOrOut(Enum):
    """Enumeration of the possible combinations of input / output files that can be preserved by the deleter."""

    INPUTS = 1
    OUTPUTS = 2
    INPUTS_OUTPUTS = 3
    NONE = 4


class Deleter:
    """Delete the requesting files and directories, with the option of preserving inputs/outputs."""

    def __init__(self, workflow: Workflow):
        """Initialize the Deleter.

        :param workflow: Workflow whose files are to be deleted
        """
        self.specification = workflow.reana_specification
        self.workflow_id = str(workflow.id_)
        self.workspace = pathlib.Path(workflow.workspace_path)

    def is_input_output_check(
        self, to_check: InOrOut, file_or_dir: Union[str, Path]
    ) -> bool:
        """Check whether the file/directory is an input, an output or both.

        :param file_or_dir: Relative path to the file/directory.
        :param to_check: Should the function check if the file is an input, an output, or both?
        """
        keys_to_check = []
        if to_check == InOrOut.NONE:
            return False
        if to_check == InOrOut.INPUTS:
            keys_to_check = ["inputs"]
        if to_check == InOrOut.OUTPUTS:
            keys_to_check = ["outputs"]
        if to_check == InOrOut.INPUTS_OUTPUTS:
            keys_to_check = ["inputs", "outputs"]
        file_or_dir = Path(file_or_dir)
        for key in keys_to_check:
            files = self.specification.get(key, {}).get("files", [])
            directories = self.specification.get(key, {}).get("directories", [])
            for file in files:
                if file_or_dir == Path(file):
                    return True
            for directory in directories:
                if is_relative_to(file_or_dir, Path(directory)):
                    return True
        return False

    def is_input(self, file_or_dir: Union[str, Path]) -> bool:
        """Check whether a file is an input or not."""
        return self.is_input_output_check(InOrOut.INPUTS, file_or_dir)

    def is_output(self, file_or_dir: Union[str, Path]) -> bool:
        """Check whether a file is an output or not."""
        return self.is_input_output_check(InOrOut.OUTPUTS, file_or_dir)

    def delete_files(self, which_to_keep: InOrOut, file_or_dir: Union[str, Path]):
        """Delete a given a file/directory, with the option to keep inputs and/or outputs.

        :param which_to_keep: Which files should be preserved? Inputs, outputs, both, or none?
        :param file_or_dir: Relative path to the file/directory.
        """
        if which_to_keep in (InOrOut.INPUTS, InOrOut.INPUTS_OUTPUTS) and self.is_input(
            file_or_dir
        ):
            click.echo(f"Preserved in: {file_or_dir}")
            return
        if which_to_keep in (
            InOrOut.OUTPUTS,
            InOrOut.INPUTS_OUTPUTS,
        ) and self.is_output(file_or_dir):
            click.echo(f"Preserved out: {file_or_dir}")
            return

        try:
            if workspace.is_directory(self.workspace, file_or_dir):
                for path in workspace.iterdir(self.workspace, file_or_dir):
                    self.delete_files(which_to_keep, path)
        except (NotADirectoryError, FileNotFoundError):
            # path refers to a file, or it has already been deleted
            pass

        try:
            workspace.delete(self.workspace, file_or_dir)
        except FileNotFoundError:
            # path has already been deleted
            pass
        except OSError as e:
            # do not raise when path refers to a non-empty directory
            if e.errno != errno.ENOTEMPTY:
                raise
        else:
            click.echo(f"Deleted: {file_or_dir}")
