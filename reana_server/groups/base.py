# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Group backend interface and canonical group reference."""

import abc
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class GroupRef:
    """Canonical, provider-neutral reference to an external group.

    Mirrors the ``external_group`` table: ``provider`` namespaces the
    immutable ``external_id`` so that same-named groups from different
    backends can never collide. ``path`` is the optional human-readable
    location (e.g. Keycloak ``/local/atlas``) surfaced by group search.
    """

    provider: str
    external_id: str
    display_name: str
    path: Optional[str] = None


class GroupClaimError(Exception):
    """The backend's group claim is absent or malformed.

    The sync engine treats this fail-closed: the user's memberships for
    this provider are cleared (see ``AUTH_ARCHITECTURE.md`` §5.7).
    """


class GroupBackendError(Exception):
    """The backend could not be reached or returned an invalid response.

    Unlike :class:`GroupClaimError` this is *not* authoritative about the
    user's memberships: the existing snapshot is kept and ages out via
    ``REANA_GROUP_MEMBERSHIP_MAX_AGE``.
    """


class GroupBackend(abc.ABC):
    """Interface implemented by every group backend.

    Implementations must stamp every emitted :class:`GroupRef` with their
    ``provider`` tag and must not write to the database — the sync engine
    in :mod:`reana_server.groups.sync` owns all writes.
    """

    #: Provider tag stamped on every GroupRef this backend emits.
    provider: str

    @abc.abstractmethod
    def extract_memberships(self, userinfo: dict) -> List[GroupRef]:
        """Parse the user's memberships out of a userinfo response.

        Called at login/JIT provisioning. Backends may do provider lookups
        when the userinfo carries only an identity/alias and the membership
        list must be fetched from the provider (e.g. the CERN Authorization
        Service resolving a UPN to its groups).

        :raises GroupClaimError: when the claim is absent or malformed
            (fail-closed in the sync engine: the snapshot is cleared).
        :raises GroupBackendError: when a provider lookup fails for
            transport/identity reasons (the sync engine keeps the existing
            snapshot to age out, rather than clearing it).
        """

    def extract_memberships_for_user(self, user, userinfo: dict) -> List[GroupRef]:
        """Like :meth:`extract_memberships` but may use the provisioned user.

        Backends that can fetch memberships more efficiently via a user
        identity (e.g. Keycloak Admin API ``GET /users/{id}/groups``) should
        override this. The default falls back to ``extract_memberships``.

        :raises GroupClaimError: same semantics as :meth:`extract_memberships`.
        :raises GroupBackendError: same semantics as :meth:`extract_memberships`.
        """
        return self.extract_memberships(userinfo)

    @abc.abstractmethod
    def fetch_memberships(self, user) -> List[GroupRef]:
        """Fetch the user's memberships live from the provider.

        Used by the periodic refresh job for users who rarely log in
        (CLI-only users). ``user`` is a ``reana_db.models.User`` with a
        linked IdP identity.

        :raises GroupBackendError: on transport/provider failures.
        """

    @abc.abstractmethod
    def search_groups(self, query: str, limit: int = 20) -> List[GroupRef]:
        """Search groups by name for the sharing UI.

        Minimum query length and rate limiting are enforced by the REST
        endpoint, not here.

        :raises GroupBackendError: on transport/provider failures.
        """

    @abc.abstractmethod
    def group_exists(self, external_id: str) -> bool:
        """Check that a group exists, used at share-creation time.

        :raises GroupBackendError: on transport/provider failures.
        """
