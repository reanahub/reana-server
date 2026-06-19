# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""CERN group backend (CERN Authorization Service / GMS API).

CERN does not expose a usable group-membership claim by default, so this
backend sources memberships from the CERN Authorization Service API
(https://authorization-service-api.web.cern.ch/, swagger at ``/swagger``)
rather than from a token/userinfo claim. The authoritative route is::

    GET /api/v1.0/Identity/{idOrUpn}/groups/recursive

which returns the identity's effective (direct *and* nested) group
membership. ``{idOrUpn}`` accepts the CERN UPN, which REANA already receives
as the ``cern_upn`` userinfo claim, so no extra identity resolution is
needed at login. The same route serves the periodic refresh job using the
UPN stored on the REANA user. Group existence (share-time validation) uses
``GET /api/v1.0/Group/{groupIdentifier}`` and share-target search uses
``GET /api/v1.0/Group?filter=...``.

``GroupRef.external_id`` is the CERN ``groupIdentifier`` — human-readable and
documented immutable — so workflow shares survive group renames. The API is
called only at login/JIT and by the refresh job (once per identity, like the
userinfo call), never on the request authorization path, which reads the
reana-db snapshot like every other provider. Access uses a least-privilege
client-credentials token with the ``authorization-service-api`` audience,
mirroring the validated ``cern-group-poc`` client.
"""

import logging
import os
import threading
import time
from typing import List, Optional
from urllib.parse import quote

import requests

from reana_server.groups.base import (
    GroupBackend,
    GroupBackendError,
    GroupRef,
)

DEFAULT_API_BASE_URL = "https://authorization-service-api.web.cern.ch/api/v1.0/"
DEFAULT_TOKEN_URL = "https://auth.cern.ch/auth/realms/cern/api-access/token"
DEFAULT_AUDIENCE = "authorization-service-api"
DEFAULT_IDENTITY_CLAIM = "cern_upn"
DEFAULT_SEARCH_FILTER_TEMPLATE = "groupIdentifier:contains:{query}"
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_GROUPS = 5000


class CernGroupBackend(GroupBackend):
    """Group backend backed by the CERN Authorization Service API."""

    def __init__(self, config):
        """Initialize from one ``REANA_GROUP_BACKENDS`` entry.

        Expected keys: ``provider`` (tag, default ``cern``); the API client
        ``client_id`` plus a secret in the environment variable named by
        ``client_secret_env`` (default
        ``REANA_GROUP_BACKEND_<PROVIDER>_CLIENT_SECRET``) — both are required
        for the backend to function. ``identity_claim`` (userinfo claim
        carrying the CERN UPN, default ``cern_upn``) and ``identity_user_attr``
        (REANA ``User`` attribute holding the UPN for the refresh job, default
        ``username``) select the identity key. The API endpoints
        (``api_base_url``, ``token_url``, ``audience``,
        ``search_filter_template``), ``page_size``, ``max_groups`` and
        ``http_timeout`` default to CERN's production values.
        """
        self.provider = config.get("provider", "cern")
        self.identity_claim = config.get("identity_claim", DEFAULT_IDENTITY_CLAIM)
        self.identity_user_attr = config.get("identity_user_attr", "username")
        self.api_base_url = config.get("api_base_url", DEFAULT_API_BASE_URL)
        self.token_url = config.get("token_url", DEFAULT_TOKEN_URL)
        self.audience = config.get("audience", DEFAULT_AUDIENCE)
        self.client_id = config.get("client_id", "")
        secret_env = config.get(
            "client_secret_env",
            f"REANA_GROUP_BACKEND_{self.provider.upper()}_CLIENT_SECRET",
        )
        self.client_secret = os.getenv(secret_env, "")
        self.search_filter_template = config.get(
            "search_filter_template", DEFAULT_SEARCH_FILTER_TEMPLATE
        )
        self.page_size = config.get("page_size", DEFAULT_PAGE_SIZE)
        self.max_groups = config.get("max_groups", DEFAULT_MAX_GROUPS)
        self.http_timeout = config.get("http_timeout", 10)
        self._token_lock = threading.Lock()
        self._token = None
        self._token_expires_at = 0.0

    @property
    def gms_enabled(self) -> bool:
        """Whether the API client credentials are configured."""
        return bool(self.client_id and self.client_secret)

    def _require_gms(self):
        if not self.gms_enabled:
            raise GroupBackendError(
                "CERN Authorization Service API client is not configured "
                f"(provider {self.provider!r}); set client_id and the secret "
                "in its client_secret_env."
            )

    # -- identity resolution ----------------------------------------------

    def _identity_key_from_userinfo(self, userinfo: dict) -> Optional[str]:
        """Return the CERN identity key (UPN) from a userinfo response."""
        for claim in (self.identity_claim, "cern_upn", "preferred_username", "sub"):
            value = userinfo.get(claim)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _identity_key_from_user(self, user) -> Optional[str]:
        """Return the CERN identity key (UPN) stored on a REANA user."""
        value = getattr(user, self.identity_user_attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if user.idp_subject:
            return user.idp_subject
        return None

    def extract_memberships(self, userinfo: dict) -> List[GroupRef]:
        """Resolve memberships at login/JIT from the user's userinfo.

        The identity (UPN) comes from userinfo; the membership list itself is
        fetched live from the Authorization Service. A failure here raises
        :class:`GroupBackendError` so the sync engine keeps the existing
        snapshot rather than clearing it on a transient API/identity problem.
        """
        self._require_gms()
        identity_key = self._identity_key_from_userinfo(userinfo)
        if not identity_key:
            raise GroupBackendError(
                "Could not determine the CERN identity from userinfo "
                f"(looked for {self.identity_claim!r}, 'preferred_username', "
                "'sub')."
            )
        return self._fetch_recursive_groups(identity_key)

    def fetch_memberships(self, user) -> List[GroupRef]:
        """Fetch a user's effective memberships for the periodic refresh job."""
        self._require_gms()
        identity_key = self._identity_key_from_user(user)
        if not identity_key:
            raise GroupBackendError(
                f"User {user.id_} has no CERN identity key "
                f"(attribute {self.identity_user_attr!r})."
            )
        return self._fetch_recursive_groups(identity_key)

    # -- Authorization Service API ----------------------------------------

    def _get_access_token(self):
        with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            try:
                response = requests.post(
                    self.token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "audience": self.audience,
                    },
                    headers={"Accept": "application/json"},
                    timeout=self.http_timeout,
                )
                response.raise_for_status()
                token_data = response.json()
                token = token_data["access_token"]
            except (requests.RequestException, ValueError, KeyError) as error:
                raise GroupBackendError(f"Could not obtain CERN API token: {error}")
            self._token = token
            # Refresh slightly before expiry.
            self._token_expires_at = (
                time.monotonic() + token_data.get("expires_in", 60) - 30
            )
            return self._token

    def _gms_get(self, path, params=None):
        url = self.api_base_url.rstrip("/") + "/" + path.lstrip("/")
        try:
            response = requests.get(
                url,
                params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._get_access_token()}",
                },
                timeout=self.http_timeout,
            )
        except requests.RequestException as error:
            raise GroupBackendError(f"CERN API request failed: {error}")
        if response.status_code == 404:
            return None
        if response.status_code in (401, 403):
            raise GroupBackendError(
                f"CERN API denied access ({response.status_code}); check the "
                "service account's read permissions."
            )
        if response.status_code != 200:
            raise GroupBackendError(
                f"CERN API returned {response.status_code} for {path}."
            )
        try:
            return response.json()
        except ValueError as error:
            raise GroupBackendError(f"CERN API returned invalid JSON: {error}")

    @staticmethod
    def _data_list(payload):
        """Return the ``data`` array of an enumerate envelope, or ``[]``."""
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", [])
        return data if isinstance(data, list) else []

    def _group_ref(self, group_identifier, display_name=None) -> Optional[GroupRef]:
        external_id = (group_identifier or "").strip()
        if not external_id:
            return None
        return GroupRef(
            provider=self.provider,
            external_id=external_id,
            display_name=(display_name or external_id),
        )

    def _fetch_recursive_groups(self, identity_key) -> List[GroupRef]:
        """Page through an identity's recursive group membership."""
        refs = []
        offset = 0
        while True:
            envelope = self._gms_get(
                f"Identity/{quote(identity_key, safe='')}/groups/recursive",
                params={"offset": offset, "limit": self.page_size},
            )
            if envelope is None:
                raise GroupBackendError(f"CERN identity {identity_key!r} not found.")
            page = self._data_list(envelope)
            for item in page:
                ref = self._group_ref(
                    item.get("groupIdentifier"),
                    display_name=item.get("displayName"),
                )
                if ref is not None:
                    refs.append(ref)
            if len(page) < self.page_size or offset >= self.max_groups:
                return refs
            offset += self.page_size

    def search_groups(self, query: str, limit: int = 20) -> List[GroupRef]:
        """Search CERN groups by identifier for the sharing UI."""
        self._require_gms()
        envelope = self._gms_get(
            "Group",
            params={
                "filter": self.search_filter_template.format(query=query),
                "limit": limit,
            },
        )
        refs = []
        for item in self._data_list(envelope)[:limit]:
            ref = self._group_ref(
                item.get("groupIdentifier"),
                display_name=item.get("displayName"),
            )
            if ref is not None:
                refs.append(ref)
        return refs

    def group_exists(self, external_id: str) -> bool:
        """Validate a group at share time by its ``groupIdentifier``."""
        self._require_gms()
        result = self._gms_get(f"Group/{quote(external_id, safe='')}")
        if result is None:
            logging.info(
                "CERN group %r does not exist (share-time validation).",
                external_id,
            )
            return False
        return True
