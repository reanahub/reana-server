# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA utilities to apply retention rules."""


from pathlib import Path
from typing import Dict, Union

import click
from reana_db.models import WorkspaceRetentionRule

from reana_server.utils import is_relative_to


class RetentionRuleDeleter:
    """Delete files and directories matching the given retention rule."""

    def __init__(self, rule: WorkspaceRetentionRule):
        """Initialize the RetentionRuleDeleter.

        :param rule: Retention rule to be applied.
        """
        self.rule_id = rule.id_
        self.specification = rule.workflow.reana_specification
        self.workflow_id = rule.workflow.id_
        self.workspace = Path(rule.workflow.workspace_path)
        self.workspace_files = rule.workspace_files

    def is_input_output(self, file_or_dir: Path) -> bool:
        """Check whether the file/directory is an input/output or not.

        :param file_or_dir: Path to the file/directory.
        """
        for key in ("inputs", "outputs"):
            files = self.specification.get(key, {}).get("files", [])
            directories = self.specification.get(key, {}).get("directories", [])
            for file in files:
                if file_or_dir == self.workspace / file:
                    return True
            for directory in directories:
                if is_relative_to(file_or_dir, self.workspace / directory):
                    return True
        return False

    def is_inside_workspace(self, file_or_dir: Path) -> bool:
        """Check if given file/directory is inside the workspace.

        :param file_or_dir: Path to the file/directory.
        """
        return is_relative_to(file_or_dir.resolve(), self.workspace.resolve())

    def delete_keeping_inputs_outputs(self, file_or_dir: Path):
        """Delete the given file/directory, keeping the inputs/outputs.

        :param file_or_dir: Path to the file/directory.
        """
        if not self.is_inside_workspace(file_or_dir):
            click.echo(f"Path outside workspace: {file_or_dir}")
        elif self.is_input_output(file_or_dir):
            click.echo(f"Preserved in/out: {file_or_dir}")
        elif file_or_dir.is_file() or file_or_dir.is_symlink():
            file_or_dir.unlink()
            click.echo(f"Deleted file: {file_or_dir}")
        else:
            for child in file_or_dir.iterdir():
                self.delete_keeping_inputs_outputs(child)
            if not any(file_or_dir.iterdir()):
                file_or_dir.rmdir()
                click.echo(f"Deleted dir: {file_or_dir}")

    def apply_rule(self):
        """Delete the files/directories matching the given retention rule, keeping inputs/outputs."""
        click.secho(
            f"Applying rule {self.rule_id} to workflow {self.workflow_id}: "
            f"'{self.workspace_files}' will be deleted from '{self.workspace}'",
            fg="green",
        )
        for file_or_dir in self.workspace.glob(self.workspace_files):
            if not file_or_dir.exists():
                click.echo(" Already deleted: {file_or_dir}")
                continue
            self.delete_keeping_inputs_outputs(file_or_dir)
