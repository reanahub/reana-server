# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022, 2023 CERN.
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

from reana_server.deleter import Deleter, InOrOut


class RetentionRuleDeleter(Deleter):
    """Delete files and directories matching the given retention rule."""

    def __init__(self, rule: WorkspaceRetentionRule):
        """Initialize the RetentionRuleDeleter.

        :param rule: Retention rule to be applied.
        """
        super().__init__(rule.workflow)
        self.rule_id = str(rule.id_)
        self.workspace_files = str(rule.workspace_files)

    def is_input_output(self, file_or_dir: Union[str, Path]) -> bool:
        """Check whether the file/directory is an input/output or not.

        :param file_or_dir: Relative path to the file/directory.
        """
        return self.is_input_output_check(InOrOut.INPUTS_OUTPUTS, file_or_dir)

    def delete_keeping_inputs_outputs(self, file_or_dir: str):
        """Delete the given file/directory, keeping the inputs/outputs.

        :param file_or_dir: Relative path to the file/directory.
        """
        self.delete_files(InOrOut.INPUTS_OUTPUTS, file_or_dir)

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
