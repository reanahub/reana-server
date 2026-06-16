# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""User-facing endpoints (authenticated)."""

from typing import List, Optional

from fastapi import APIRouter, Request, Security
from pydantic import BaseModel
from reana_db.models import User

from reana_server.auth.deps import get_current_user

router = APIRouter(tags=["users"])


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
