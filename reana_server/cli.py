# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018, 2019, 2020, 2021 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Server command line tool."""

import logging
import signal

import click
from reana_commons.config import REANA_LOG_FORMAT, REANA_LOG_LEVEL

from reana_server.scheduler import WorkflowExecutionScheduler


@click.command("start-scheduler")
def start_scheduler():
    """Start a workflow execution scheduler process."""
    logging.basicConfig(level=REANA_LOG_LEVEL, format=REANA_LOG_FORMAT, force=True)
    scheduler = WorkflowExecutionScheduler()

    def stop_scheduler(signum, frame):
        logging.info("Stopping scheduler...")
        scheduler.should_stop = True

    signal.signal(signal.SIGTERM, stop_scheduler)

    logging.info("Starting scheduler...")
    scheduler.run()
