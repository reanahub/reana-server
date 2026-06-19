# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""User secrets endpoints (Kubernetes-backed user secret store)."""

import logging
import traceback
from typing import List

from fastapi import APIRouter, Body, Query, Security
from fastapi.responses import JSONResponse
from reana_commons.errors import REANASecretAlreadyExists, REANASecretDoesNotExist
from reana_commons.k8s.secrets import Secret, UserSecretsStore
from reana_db.models import User

from reana_server.auth.deps import get_current_user

router = APIRouter(tags=["secrets"])

_RoleUser = Security(get_current_user, scopes=["reana:user"])


@router.post("/secrets/", status_code=201, summary="Add user secrets")
def add_secrets(
    payload: dict = Body(...),
    overwrite: bool = Query(False),
    user: User = _RoleUser,
):
    """Add base64-encoded secrets ``{name: {value, type}}`` to the user store."""
    try:
        secrets = [
            Secret.from_base64(
                name=name, value=secret["value"], type_=secret["type"]
            )
            for name, secret in payload.items()
        ]
    except (ValueError, KeyError, TypeError) as error:
        return JSONResponse({"message": str(error)}, 400)
    try:
        user_secrets = UserSecretsStore.fetch(user.id_)
        user_secrets.add_secrets(secrets, overwrite=overwrite)
        UserSecretsStore.update(user_secrets)
        return JSONResponse({"message": "Secret(s) successfully added."}, 201)
    except REANASecretAlreadyExists as error:
        return JSONResponse({"message": str(error)}, 409)
    except ValueError:
        return JSONResponse({"message": "Token is not valid."}, 403)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)


@router.get("/secrets", summary="List user secrets")
def get_secrets(user: User = _RoleUser):
    """List the user's secret names and types (never the values)."""
    try:
        user_secrets = UserSecretsStore.fetch(user.id_)
        return [
            {"name": secret.name, "type": secret.type_}
            for secret in user_secrets.get_secrets()
        ]
    except ValueError:
        return JSONResponse({"message": "Token is not valid."}, 403)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)


@router.delete("/secrets/", summary="Delete user secrets")
def delete_secrets(payload: List[str] = Body(...), user: User = _RoleUser):
    """Delete the named secrets from the user store."""
    try:
        user_secrets = UserSecretsStore.fetch(user.id_)
        deleted = user_secrets.delete_secrets(payload)
        UserSecretsStore.update(user_secrets)
        return deleted
    except REANASecretDoesNotExist as error:
        return JSONResponse(error.missing_secrets_list, 404)
    except ValueError:
        return JSONResponse({"message": "Token is not valid."}, 403)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)
