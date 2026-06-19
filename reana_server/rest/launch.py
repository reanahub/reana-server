# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Launch-from-URL endpoint (fetch a remote spec, create and submit)."""

import json
import logging
import os
import shutil
import threading
import traceback

import yaml
from bravado.exception import HTTPError
from fastapi import APIRouter, Body, Security
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from jsonschema import ValidationError
from reana_commons.errors import REANAQuotaExceededError, REANAValidationError
from reana_commons.specification import load_reana_spec
from reana_commons.validation.utils import validate_workflow_name
from reana_db.models import User
from reana_db.utils import (
    _get_workflow_with_uuid_or_name,
    get_disk_usage_or_zero,
    store_workflow_disk_quota,
    update_users_disk_quota,
)

from reana_server.api_client import current_rwc_api_client
from reana_server.auth.deps import get_current_user
from reana_server.config import LAUNCHER_ALLOWED_SNAKEMAKE_URLS
from reana_server.fetcher import REANAFetcherError, get_fetcher
from reana_server.utils import (
    filter_input_files,
    get_fetched_workflows_dir,
    get_quota_excess_message,
    get_workspace_retention_rules,
    mv_workflow_files,
    prevent_disk_quota_excess,
    publish_workflow_submission,
)
from reana_server.validation import validate_workflow

router = APIRouter(tags=["launch"])

load_reana_spec_lock = threading.Lock()
"""Lock so only one specification is loaded at a time (it changes the cwd)."""


@router.post("/launch", summary="Launch a workflow from a remote URL")
def launch(
    payload: dict = Body(...),
    user: User = Security(get_current_user, scopes=["reana:user"]),
):
    """Fetch a REANA spec from a URL, create the workflow and submit it."""
    if user.has_exceeded_quota():
        return JSONResponse({"message": get_quota_excess_message(user)}, 403)
    url = payload.get("url")
    if not url:
        return JSONResponse({"message": "Field 'url' is required."}, 400)
    name = payload.get("name", "")
    parameters = payload.get("parameters", "{}")
    specification = payload.get("specification")

    tmpdir = None
    try:
        user_id = str(user.id_)
        tmpdir = get_fetched_workflows_dir(user_id)

        fetcher = get_fetcher(url, tmpdir, specification)
        fetcher.fetch()

        workflow_name = name.replace(" ", "") or fetcher.generate_workflow_name()
        validate_workflow_name(workflow_name)

        spec_path = fetcher.workflow_spec_path()
        with open(spec_path) as spec_fd:
            reana_yaml = yaml.safe_load(spec_fd.read())
            if (
                reana_yaml["workflow"]["type"] == "snakemake"
                and url not in LAUNCHER_ALLOWED_SNAKEMAKE_URLS
            ):
                raise ValidationError(
                    "Unfortunately, it is not possible to launch generic "
                    "Snakemake workflows at the moment. Please contact the "
                    "REANA admins for more information."
                )

        with load_reana_spec_lock:
            reana_yaml = load_reana_spec(spec_path, workspace_path=tmpdir)
        input_parameters = json.loads(parameters)
        validation_warnings = validate_workflow(reana_yaml, input_parameters)

        filter_input_files(tmpdir, reana_yaml)
        disk_usage = get_disk_usage_or_zero(tmpdir)
        prevent_disk_quota_excess(
            user, disk_usage, action=f"Launching the workflow {workflow_name}"
        )
        retention_rules = get_workspace_retention_rules(
            reana_yaml.get("workspace", {}).get("retention_days")
        )
        workflow_dict = {
            "reana_specification": reana_yaml,
            "workflow_name": workflow_name,
            "operational_options": {},
            "launcher_url": url,
            "retention_rules": retention_rules,
        }
        response, _ = current_rwc_api_client.api.create_workflow(
            workflow=workflow_dict, user=user_id
        ).result()

        workflow = _get_workflow_with_uuid_or_name(
            response["workflow_id"], user_id
        )
        mv_workflow_files(tmpdir, workflow.workspace_path)
        store_workflow_disk_quota(workflow, bytes_to_sum=disk_usage)
        update_users_disk_quota(user, bytes_to_sum=disk_usage)

        publish_workflow_submission(
            workflow, user.id_, {"input_parameters": input_parameters}
        )
        response_data = {
            "workflow_id": str(workflow.id_),
            "workflow_name": workflow.name,
            "message": "The workflow has been successfully submitted.",
        }
        if validation_warnings:
            response_data["message"] = (
                "The workflow has been successfully submitted, but some "
                "warnings were issued."
            )
            response_data["validation_warnings"] = validation_warnings
        return jsonable_encoder(response_data)
    except HTTPError as error:
        return JSONResponse(error.response.json(), error.response.status_code)
    except json.JSONDecodeError:
        return JSONResponse(
            {"message": "The workflow 'parameters' field is not valid JSON."},
            400,
        )
    except REANAQuotaExceededError as error:
        return JSONResponse({"message": str(error)}, 403)
    except (REANAFetcherError, REANAValidationError, ValueError, ValidationError) as error:
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 400)
    except Exception:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse(
            {"message": "Something went wrong while fetching the workflow."}, 500
        )
    finally:
        # See the Flask version: only clear the directory contents (not the
        # directory itself, since load_reana_spec changes the cwd).
        if tmpdir:
            for entry in os.scandir(tmpdir):
                if entry.is_file() or entry.is_symlink():
                    os.remove(entry)
                else:
                    shutil.rmtree(entry)
