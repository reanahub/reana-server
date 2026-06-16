# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Workflow endpoints (role-gated).

MVP: a single role-gated listing that proves the ``reana:user`` scope path
end to end. The real listing — a call to reana-workflow-controller plus the
group-aware ``shared_with_me`` filter (``reana_db.utils`` helpers) — is the
next thing to wire here; that work needs the reana-commons httpx client
(RC-1), which is out of MVP scope.
"""

from typing import List

from fastapi import APIRouter, Security
from pydantic import BaseModel
from reana_db.models import User

from reana_server.auth.deps import get_current_user

router = APIRouter(tags=["workflows"])


class WorkflowList(BaseModel):
    """Paginated workflow listing (MVP shape)."""

    items: List[dict] = []
    total: int = 0


@router.get(
    "/workflows",
    response_model=WorkflowList,
    summary="List workflows (role-gated MVP stub)",
)
async def list_workflows(
    # ``scopes=["reana:user"]``: requires the REANA role in the token.
    user: User = Security(get_current_user, scopes=["reana:user"]),
) -> WorkflowList:
    """Return the caller's workflows.

    MVP stub returning an empty page: it exercises the role-gated JWT
    dependency without yet calling reana-workflow-controller.
    """
    return WorkflowList(items=[], total=0)
