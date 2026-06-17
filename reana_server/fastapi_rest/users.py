# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""User-facing endpoints (authenticated)."""

import logging
import traceback
from typing import List, Optional

from fastapi import APIRouter, Request, Security
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from reana_db.database import Session
from reana_db.models import User, UserWorkflow, Workflow

from reana_server.auth.deps import get_current_user

router = APIRouter(tags=["users"])

_RoleUser = Security(get_current_user, scopes=["reana:user"])


class Identity(BaseModel):
    """The caller's external IdP identity ``(iss, sub)``."""

    issuer: Optional[str] = None
    subject: Optional[str] = None


class You(BaseModel):
    """Authenticated user info (``auth_contract_freeze.md`` §User).

    Deliberately excludes tokens, secrets and full group lists.
    """

    id: str
    email: str
    full_name: Optional[str] = None
    username: Optional[str] = None
    roles: List[str] = []
    identity: Identity


@router.get("/you", response_model=You, summary="Authenticated user info")
async def get_you(
    request: Request,
    # ``scopes=[]``: role-optional, like the legacy ``/you`` — any valid
    # identity of the trusted issuer may see its own "access not granted"
    # state. (First-time identities are still role-gated at provisioning.)
    user: User = Security(get_current_user, scopes=[]),
) -> You:
    """Return the calling user's REANA identity, roles and IdP subject."""
    roles = getattr(request.state, "reana_roles", [])
    return You(
        id=str(user.id_),
        email=user.email,
        full_name=user.full_name,
        username=user.username,
        roles=roles,
        identity=Identity(issuer=user.idp_issuer, subject=user.idp_subject),
    )


@router.get(
    "/users/shared-with-you", summary="Users who shared workflows with you"
)
def get_users_shared_with_you(user: User = _RoleUser):
    """List the users who have shared at least one workflow with the caller."""
    try:
        shared_workflows_ids = (
            Session.query(UserWorkflow.workflow_id)
            .filter(UserWorkflow.user_id == user.id_)
            .subquery()
        )
        owners_ids = (
            Session.query(Workflow.owner_id)
            .filter(Workflow.id_.in_(shared_workflows_ids))
            .subquery()
        )
        users = (
            Session.query(User.email)
            .filter(User.id_.in_(owners_ids))
            .all()
        )
        return {"users_shared_with_you": [{"email": u.email} for u in users]}
    except ValueError as error:
        return JSONResponse({"message": str(error)}, 403)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)


@router.get(
    "/users/you-shared-with", summary="Users you shared workflows with"
)
def get_users_you_shared_with(user: User = _RoleUser):
    """List the users with whom the caller has shared at least one workflow."""
    try:
        owned_workflows_ids = (
            Session.query(Workflow.id_)
            .filter(Workflow.owner_id == user.id_)
            .subquery()
        )
        shared_with_ids = (
            Session.query(UserWorkflow.user_id)
            .filter(UserWorkflow.workflow_id.in_(owned_workflows_ids))
            .distinct()
            .subquery()
        )
        users = (
            Session.query(User.email)
            .filter(User.id_.in_(shared_with_ids))
            .all()
        )
        return {"users_you_shared_with": [{"email": u.email} for u in users]}
    except ValueError as error:
        return JSONResponse({"message": str(error)}, 403)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)
