# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA utilities to apply retention rules."""


import errno
from pathlib import Path
from typing import Union

import click
from reana_commons import workspace
from reana_db.models import WorkspaceRetentionRule

from reana_server.utils import is_relative_to


class RetentionRuleDeleter:
    """Delete files and directories matching the given retention rule."""

    def __init__(self, rule: WorkspaceRetentionRule):
        """Initialize the RetentionRuleDeleter.

        :param rule: Retention rule to be applied.
        """
        self.rule_id = str(rule.id_)
        self.specification = rule.workflow.reana_specification
        self.workflow_id = str(rule.workflow.id_)
        self.workspace = str(rule.workflow.workspace_path)
        self.workspace_files = str(rule.workspace_files)

    def is_input_output(self, file_or_dir: Union[str, Path]) -> bool:
        """Check whether the file/directory is an input/output or not.

        :param file_or_dir: Relative path to the file/directory.
        """
        file_or_dir = Path(file_or_dir)
        for key in ("inputs", "outputs"):
            files = self.specification.get(key, {}).get("files", [])
            directories = self.specification.get(key, {}).get("directories", [])
            for file in files:
                if file_or_dir == Path(file):
                    return True
            for directory in directories:
                if is_relative_to(file_or_dir, Path(directory)):
                    return True
        return False

    def delete_keeping_inputs_outputs(self, file_or_dir: str):
        """Delete the given file/directory, keeping the inputs/outputs.

        :param file_or_dir: Relative path to the file/directory.
        """
        if self.is_input_output(file_or_dir):
            click.echo(f"Preserved in/out: {file_or_dir}")
            return

        try:
            if workspace.is_directory(self.workspace, file_or_dir):
                for path in workspace.iterdir(self.workspace, file_or_dir):
                    self.delete_keeping_inputs_outputs(path)
        except (NotADirectoryError, FileNotFoundError):
            # path refers to a file or it has already been deleted
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

    def apply_rule(self, dry_run: bool = False):
        """Delete the files/directories matching the given retention rule, keeping inputs/outputs."""
        click.secho(
            f"Applying rule {self.rule_id} to workflow {self.workflow_id}: "
            f"'{self.workspace_files}' will be deleted from '{self.workspace}'",
            fg="green",
        )
        if dry_run:
            return
        for file_or_dir in workspace.glob(self.workspace, self.workspace_files):
            self.delete_keeping_inputs_outputs(file_or_dir)
