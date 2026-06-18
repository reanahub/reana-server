# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Group membership sync engine.

The single owner of all group-related database writes. Backends produce
normalized :class:`reana_server.groups.base.GroupRef` lists; this module
validates, deduplicates, caps and diffs them against the reana-db snapshot
in one transaction per (user, provider).

Failure semantics (AUTH_ARCHITECTURE.md §5.7):

- absent/malformed claim (``GroupClaimError``) ⇒ fail-closed: the user's
  memberships for that provider are cleared;
- backend transport failure (``GroupBackendError``) ⇒ the existing
  snapshot is kept and ages out via ``REANA_GROUP_MEMBERSHIP_MAX_AGE``.
"""

import logging
import re
from datetime import datetime
from typing import List

from reana_db.database import Session
from reana_db.models import ExternalGroup, UserGroupMembership

from reana_server.groups import get_group_backends
from reana_server.groups.base import (
    GroupBackendError,
    GroupClaimError,
    GroupRef,
)

GROUP_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9/][A-Za-z0-9/_.:@+\- ]{0,254}$")
"""Accepted group identifier charset.

Extends the rule validated in ``cern-group-poc`` with ``/`` (Keycloak and
IAM paths) and inner spaces (Keycloak group names).
"""

MAX_GROUPS_PER_SYNC = 5000
"""Upper bound of memberships synchronized per user and provider."""


def _normalize_refs(refs: List[GroupRef], provider: str) -> List[GroupRef]:
    """Validate, deduplicate and cap a backend's GroupRef list."""
    seen = {}
    for ref in refs:
        if ref.provider != provider:
            logging.warning(
                "Dropping group %r: provider %r does not match backend %r.",
                ref.external_id,
                ref.provider,
                provider,
            )
            continue
        if not GROUP_IDENTIFIER_RE.fullmatch(ref.external_id or ""):
            logging.warning(
                "Dropping invalid group identifier %r (provider %r).",
                ref.external_id,
                provider,
            )
            continue
        seen.setdefault(ref.external_id, ref)
        if len(seen) >= MAX_GROUPS_PER_SYNC:
            logging.warning(
                "Group list for provider %r capped at %d entries.",
                provider,
                MAX_GROUPS_PER_SYNC,
            )
            break
    return sorted(seen.values(), key=lambda r: r.external_id.casefold())


def sync_user_groups(user, provider: str, refs: List[GroupRef]) -> None:
    """Replace the user's membership snapshot for one provider.

    Upserts the referenced ``external_group`` rows, diffs the user's
    memberships for this provider only (other providers are never
    touched), bulk-applies the changes and stamps ``synced_at``, all in
    one transaction.
    """
    refs = _normalize_refs(refs, provider)
    now = datetime.utcnow()
    try:
        # Upsert referenced groups.
        groups_by_external_id = {}
        if refs:
            existing_groups = (
                Session.query(ExternalGroup)
                .filter(
                    ExternalGroup.provider == provider,
                    ExternalGroup.external_id.in_([ref.external_id for ref in refs]),
                )
                .all()
            )
            groups_by_external_id = {
                group.external_id: group for group in existing_groups
            }
            for ref in refs:
                group = groups_by_external_id.get(ref.external_id)
                if group is None:
                    group = ExternalGroup(
                        provider=provider,
                        external_id=ref.external_id,
                        display_name=ref.display_name,
                        last_seen_at=now,
                    )
                    Session.add(group)
                    groups_by_external_id[ref.external_id] = group
                else:
                    group.display_name = ref.display_name
                    group.last_seen_at = now
            Session.flush()

        # Diff memberships for this provider only.
        desired_group_ids = {group.id_ for group in groups_by_external_id.values()}
        current_memberships = (
            Session.query(UserGroupMembership)
            .join(
                ExternalGroup,
                UserGroupMembership.group_id == ExternalGroup.id_,
            )
            .filter(
                UserGroupMembership.user_id == user.id_,
                ExternalGroup.provider == provider,
            )
            .all()
        )
        kept = 0
        for membership in current_memberships:
            if membership.group_id in desired_group_ids:
                membership.synced_at = now
                desired_group_ids.discard(membership.group_id)
                kept += 1
            else:
                Session.delete(membership)
        for group_id in desired_group_ids:
            Session.add(
                UserGroupMembership(user_id=user.id_, group_id=group_id, synced_at=now)
            )
        Session.commit()
        logging.info(
            "Synced %d group membership(s) for user %s (provider %r: "
            "%d kept, %d added, %d removed).",
            len(refs),
            user.id_,
            provider,
            kept,
            len(desired_group_ids),
            len(current_memberships) - kept,
        )
    except Exception:
        Session.rollback()
        raise


def clear_user_groups(user, provider: str) -> None:
    """Remove the user's membership snapshot for one provider (fail-closed)."""
    sync_user_groups(user, provider, [])


def sync_user_groups_from_userinfo(user, userinfo: dict) -> None:
    """Sync all configured providers from a userinfo response.

    Called at login/JIT provisioning. A missing or malformed *claim* is
    authoritative for that provider and clears its memberships (fail-closed).
    A backend that resolves memberships via a provider lookup (e.g. the CERN
    Authorization Service) may instead raise :class:`GroupBackendError` on a
    transport/identity failure; that is *not* authoritative, so the existing
    snapshot is kept and ages out via ``REANA_GROUP_MEMBERSHIP_MAX_AGE``.
    Each provider is isolated: one provider failing never affects another.
    """
    for provider, backend in get_group_backends().items():
        try:
            refs = backend.extract_memberships(userinfo)
        except GroupClaimError as error:
            logging.warning(
                "Group claim missing/malformed for provider %r, clearing "
                "memberships of user %s (fail-closed): %s",
                provider,
                user.id_,
                error,
            )
            clear_user_groups(user, provider)
            continue
        except GroupBackendError as error:
            logging.warning(
                "Group lookup failed for provider %r, user %s; keeping "
                "existing snapshot: %s",
                provider,
                user.id_,
                error,
            )
            continue
        sync_user_groups(user, provider, refs)


def sync_user_groups_live(user) -> None:
    """Refresh all configured providers via live backend queries.

    Used by the periodic refresh job for users who rarely log in. On
    backend failure the existing snapshot is kept (it ages out via the
    membership max age) — only an authoritative response replaces it.
    """
    for provider, backend in get_group_backends().items():
        try:
            refs = backend.fetch_memberships(user)
        except GroupBackendError as error:
            logging.warning(
                "Live group refresh failed for provider %r, user %s; "
                "keeping existing snapshot: %s",
                provider,
                user.id_,
                error,
            )
            continue
        sync_user_groups(user, provider, refs)
