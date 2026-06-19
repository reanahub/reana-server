# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""EOSC AAI group backend (EOSC/MyAccessID/EGI Check-In proxy).

EOSC AAI does not expose a group-membership REST API accessible with a
service-account credential. Instead, group memberships and VO access rights
are conveyed via the ``entitlements`` userinfo claim (EOSC AAI 2025 /
AARC-G069) or the legacy ``eduperson_entitlement`` claim as a list of URNs
in the AARC-G069 format::

    urn:mace:egi.eu:group:<vo>[:<subgroup>...]:role=<role>#<authority>

Examples::

    urn:mace:egi.eu:group:vo.eosc-hub.eu:role=member#aai.egi.eu
    urn:mace:egi.eu:group:vo.eosc-hub.eu:sub-group:role=vm_operator#aai.egi.eu

This backend parses that claim at login/JIT provisioning and emits one
:class:`reana_server.groups.base.GroupRef` per matched entitlement.
``GroupRef.external_id`` is the stable group path (everything between the
``group:`` namespace prefix and the ``:role=…`` suffix), e.g.
``vo.eosc-hub.eu`` or ``vo.eosc-hub.eu:sub-group``.  The ``#<authority>``
suffix is stripped so that group records survive operator migrations between
EOSC AAI proxy instances.

Because EOSC provides no server-side membership lookup API, periodic
membership refresh is not supported: ``fetch_memberships`` raises
:class:`GroupBackendError` and the existing snapshot ages out via
``REANA_GROUP_MEMBERSHIP_MAX_AGE`` until the user re-authenticates.
Similarly, ``search_groups`` is not available; ``supports_search = False``
prevents the search endpoint from treating that as a service failure.
"""

import logging
import re
from typing import List, Optional

from reana_server.groups.base import (
    GroupBackend,
    GroupBackendError,
    GroupClaimError,
    GroupRef,
)

# AARC-G069 entitlement URN: prefix:group:<path>:role=<role>[#<authority>].
_ENTITLEMENT_RE = re.compile(
    r"^(?P<namespace>urn:.+):group:(?P<path>.+?):role=(?P<role>[^#\s]+)"
    r"(?:#(?P<authority>[^\s]+))?$"
)

DEFAULT_ENTITLEMENT_CLAIMS = ("entitlements", "eduperson_entitlement")
DEFAULT_URN_NAMESPACE = "urn:mace:egi.eu"
DEFAULT_MEMBER_ROLES = ("member",)


class EoscGroupBackend(GroupBackend):
    """Group backend for EOSC AAI / EGI Check-In (claim-only, no API calls).

    Configuration keys (all optional):

    ``provider``
        Provider tag stamped on GroupRef entries. Default: ``eosc``.
    ``entitlement_claim``
        Userinfo claim name carrying the entitlement list. Kept for backward
        compatibility; when omitted, both ``entitlements`` and
        ``eduperson_entitlement`` are accepted.
    ``entitlement_claims``
        Ordered list of userinfo claim names carrying entitlement lists.
        Default: ``["entitlements", "eduperson_entitlement"]``.
    ``urn_namespace``
        Only entitlements whose namespace prefix matches this string are
        processed. An empty string disables namespace filtering (accepts
        entitlements from any AARC-G002-compatible proxy).
        Default: ``urn:mace:egi.eu``.
    ``member_roles``
        List of entitlement role values (the ``role=<x>`` part) that count
        as group membership. Entitlements with other roles (e.g.
        ``vm_operator``, ``admin``) are ignored.
        Default: ``["member"]``.
    """

    supports_search: bool = False
    supports_live_refresh: bool = False

    def __init__(self, config):
        self.provider = config.get("provider", "eosc")
        if config.get("entitlement_claim"):
            self.entitlement_claims = [config["entitlement_claim"]]
        else:
            self.entitlement_claims = config.get(
                "entitlement_claims", list(DEFAULT_ENTITLEMENT_CLAIMS)
            )
        self.urn_namespace = config.get("urn_namespace", DEFAULT_URN_NAMESPACE)
        raw_member_roles = config.get("member_roles", list(DEFAULT_MEMBER_ROLES))
        self.member_roles = set(raw_member_roles) if raw_member_roles else set()

    # -- entitlement parsing -----------------------------------------------

    def _parse_entitlement(self, urn: str) -> Optional[GroupRef]:
        """Parse one AARC-G002 entitlement URN into a GroupRef, or None.

        Returns None for:
        - URNs that don't match the expected structure
        - Namespace mismatches (when ``urn_namespace`` is configured)
        - Role values not in ``member_roles``
        """
        match = _ENTITLEMENT_RE.match(urn.strip())
        if not match:
            return None
        if self.urn_namespace and not match.group("namespace").startswith(
            self.urn_namespace
        ):
            return None
        role = match.group("role")
        if self.member_roles and role not in self.member_roles:
            return None
        path = match.group("path").strip()
        if not path:
            return None
        return GroupRef(
            provider=self.provider,
            external_id=path,
            display_name=path,
        )

    def extract_memberships(self, userinfo: dict) -> List[GroupRef]:
        """Parse group memberships from the entitlement claim in userinfo.

        :raises GroupClaimError: when the claim is absent or not a list
            (fail-closed in the sync engine: memberships are cleared).
        """
        claim_name = next(
            (claim for claim in self.entitlement_claims if claim in userinfo),
            None,
        )
        if claim_name is None:
            raise GroupClaimError(
                "Userinfo response has none of the configured EOSC "
                f"entitlement claims {self.entitlement_claims!r}. Ensure "
                "the 'entitlements' scope is requested and the EOSC AAI "
                "client is configured to release this attribute."
            )
        raw = userinfo[claim_name]
        if not isinstance(raw, list):
            raise GroupClaimError(
                f"Userinfo claim '{claim_name}' is not a list "
                f"(got {type(raw).__name__!r})."
            )
        refs = []
        for urn in raw:
            if not isinstance(urn, str):
                continue
            ref = self._parse_entitlement(urn)
            if ref is not None:
                refs.append(ref)
        logging.debug(
            "EOSC backend: parsed %d group memberships from %d entitlements "
            "(provider %r).",
            len(refs),
            len(raw),
            self.provider,
        )
        return refs

    def fetch_memberships(self, user) -> List[GroupRef]:
        """Not supported: EOSC exposes no server-side membership lookup API.

        The periodic refresh job will keep the existing snapshot until the
        user re-authenticates and ``extract_memberships`` is called with a
        fresh userinfo response.
        """
        raise GroupBackendError(
            f"Live group refresh is not supported for the EOSC backend "
            f"(provider {self.provider!r}): EOSC AAI exposes no membership "
            "lookup API. Memberships are refreshed automatically at the next "
            "user login."
        )

    def search_groups(self, query: str, limit: int = 20) -> List[GroupRef]:
        """Not supported: EOSC has no group search API.

        ``supports_search = False`` prevents the search endpoint from calling
        this and treating the error as a service failure.
        """
        raise GroupBackendError(
            f"Group search is not supported for the EOSC backend "
            f"(provider {self.provider!r}): EOSC AAI has no group search API."
        )

    def group_exists(self, external_id: str) -> bool:
        """Validate a group external_id by checking it against the DB snapshot.

        EOSC has no group-existence API. We accept any syntactically valid
        group path; the DB snapshot (built from parsed entitlements) is the
        practical guard against nonsensical share targets.
        """
        from reana_db.database import Session
        from reana_db.models import ExternalGroup

        result = (
            Session.query(ExternalGroup)
            .filter_by(provider=self.provider, external_id=external_id)
            .one_or_none()
        )
        if result is None:
            logging.info(
                "EOSC group %r not found in DB snapshot (provider %r); "
                "it may not yet be known to this REANA instance.",
                external_id,
                self.provider,
            )
            return False
        return True
