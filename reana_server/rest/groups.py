# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Group search endpoint (share-target discovery only).

Contract (``auth_contract_freeze.md`` §Group Search): minimum query length 3,
results carry provider / stable external id / display name / optional path and
never member lists, and a backend failure returns ``503`` rather than an empty
successful result.
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Security
from pydantic import BaseModel
from reana_db.models import User

from reana_server.auth.deps import get_current_user
from reana_server.groups import get_group_backend, get_group_backends
from reana_server.groups.base import GroupBackendError

router = APIRouter(tags=["groups"])

_SEARCH_LIMIT = 20


class GroupSearchItem(BaseModel):
    """A single share-target group (no member list)."""

    provider: str
    external_id: str
    display_name: str
    path: Optional[str] = None


class GroupSearchResponse(BaseModel):
    """Group search results."""

    items: List[GroupSearchItem] = []


@router.get(
    "/groups/search",
    response_model=GroupSearchResponse,
    summary="Search groups for share-target discovery",
)
async def search_groups(
    query: str = Query(min_length=3, description="At least 3 characters."),
    provider: Optional[str] = Query(default=None),
    user: User = Security(get_current_user, scopes=["reana:user"]),
) -> GroupSearchResponse:
    """Search configured group backends for share-target candidates."""
    if provider is not None:
        backend = get_group_backend(provider)
        if backend is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown group provider '{provider}'."
            )
        backends = [backend]
    else:
        backends = list(get_group_backends().values())

    items: List[GroupSearchItem] = []
    for backend in backends:
        if not backend.supports_search:
            continue
        try:
            for ref in backend.search_groups(query, limit=_SEARCH_LIMIT):
                items.append(
                    GroupSearchItem(
                        provider=ref.provider,
                        external_id=ref.external_id,
                        display_name=ref.display_name,
                        path=ref.path,
                    )
                )
        except GroupBackendError as error:
            # A backend failure must not look like "no groups found".
            raise HTTPException(status_code=503, detail=str(error))

    return GroupSearchResponse(items=items[:_SEARCH_LIMIT])
