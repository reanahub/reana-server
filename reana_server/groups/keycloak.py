# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Keycloak group backend (local Keycloak groups, topology A).

Memberships arrive in the ``groups`` userinfo claim (Keycloak's group
membership mapper with full group paths, attached to a userinfo-only client
scope). Search, existence checks and live membership fetches use the
Keycloak Admin REST API with a least-privilege service-account client
(realm-management roles ``view-users``/``query-groups``).
"""

import logging
import os
import threading
import time
from typing import List
from urllib.parse import quote

import requests

from reana_server.groups.base import (
    GroupBackend,
    GroupBackendError,
    GroupClaimError,
    GroupRef,
)


class KeycloakGroupBackend(GroupBackend):
    """Group backend backed by a Keycloak realm's groups."""

    def __init__(self, config):
        """Initialize from one ``REANA_GROUP_BACKENDS`` entry.

        Expected keys: ``provider`` (tag, default ``keycloak``),
        ``server_url`` (e.g. ``https://auth.example.org``), ``realm``,
        ``groups_claim`` (default ``groups``), ``client_id`` (service
        account for the Admin API), ``client_secret_env`` (name of the
        environment variable holding the client secret, default
        ``REANA_GROUP_BACKEND_<PROVIDER>_CLIENT_SECRET``), ``http_timeout``.
        """
        self.provider = config.get("provider", "keycloak")
        self.server_url = config.get("server_url", "").rstrip("/")
        self.realm = config.get("realm", "reana")
        self.groups_claim = config.get("groups_claim", "groups")
        self.client_id = config.get("client_id", "")
        secret_env = config.get(
            "client_secret_env",
            f"REANA_GROUP_BACKEND_{self.provider.upper()}_CLIENT_SECRET",
        )
        self.client_secret = os.getenv(secret_env, "")
        self.http_timeout = config.get("http_timeout", 10)
        self._token_lock = threading.Lock()
        self._admin_token = None
        self._admin_token_expires_at = 0.0

    # -- claim parsing (no I/O) -------------------------------------------

    def extract_memberships(self, userinfo: dict) -> List[GroupRef]:
        """Parse Keycloak group paths from the userinfo groups claim."""
        if self.groups_claim not in userinfo:
            raise GroupClaimError(
                f"Userinfo response has no '{self.groups_claim}' claim."
            )
        raw_groups = userinfo[self.groups_claim]
        if not isinstance(raw_groups, list):
            raise GroupClaimError(
                f"Userinfo claim '{self.groups_claim}' is not a list."
            )
        return [
            self._group_ref(path)
            for path in raw_groups
            if isinstance(path, str)
        ]

    def _group_ref(self, path, display_name=None):
        path = path.strip()
        return GroupRef(
            provider=self.provider,
            external_id=path,
            display_name=display_name or path.rsplit("/", 1)[-1] or path,
            path=path,
        )

    # -- Admin REST API ----------------------------------------------------

    @property
    def _admin_base(self):
        return f"{self.server_url}/admin/realms/{self.realm}"

    def _get_admin_token(self):
        with self._token_lock:
            if (
                self._admin_token
                and time.monotonic() < self._admin_token_expires_at
            ):
                return self._admin_token
            token_url = (
                f"{self.server_url}/realms/{self.realm}"
                "/protocol/openid-connect/token"
            )
            try:
                response = requests.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=self.http_timeout,
                )
                response.raise_for_status()
                token_data = response.json()
            except (requests.RequestException, ValueError) as error:
                raise GroupBackendError(
                    f"Could not obtain Keycloak service-account token: {error}"
                )
            self._admin_token = token_data["access_token"]
            # Refresh slightly before expiry.
            self._admin_token_expires_at = (
                time.monotonic() + token_data.get("expires_in", 60) - 30
            )
            return self._admin_token

    def _admin_get(self, path, params=None, ok_statuses=(200,)):
        url = f"{self._admin_base}{path}"
        try:
            response = requests.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._get_admin_token()}"},
                timeout=self.http_timeout,
            )
        except requests.RequestException as error:
            raise GroupBackendError(
                f"Keycloak Admin API request failed: {error}"
            )
        if response.status_code == 404:
            return None
        if response.status_code not in ok_statuses:
            raise GroupBackendError(
                f"Keycloak Admin API returned {response.status_code} "
                f"for {path}."
            )
        try:
            return response.json()
        except ValueError as error:
            raise GroupBackendError(
                f"Keycloak Admin API returned invalid JSON: {error}"
            )

    def fetch_memberships(self, user) -> List[GroupRef]:
        """Fetch a user's groups via the Admin API (periodic refresh).

        In topology A the Keycloak user id equals the token ``sub``, which
        is stored as ``User.idp_subject``.
        """
        if not user.idp_subject:
            raise GroupBackendError(
                f"User {user.id_} has no linked IdP identity."
            )
        refs = []
        first = 0
        page_size = 100
        while True:
            page = self._admin_get(
                f"/users/{quote(user.idp_subject, safe='')}/groups",
                params={
                    "first": first,
                    "max": page_size,
                    "briefRepresentation": "true",
                },
            )
            if page is None:
                raise GroupBackendError(
                    f"Keycloak user {user.idp_subject} not found."
                )
            for group in page:
                refs.append(
                    self._group_ref(
                        group.get("path", group.get("name", "")),
                        display_name=group.get("name"),
                    )
                )
            if len(page) < page_size:
                return refs
            first += page_size

    def search_groups(self, query: str, limit: int = 20) -> List[GroupRef]:
        """Search realm groups by name (flattening subgroup hierarchies)."""
        tree = (
            self._admin_get(
                "/groups",
                params={
                    "search": query,
                    "max": limit,
                    "briefRepresentation": "true",
                },
            )
            or []
        )
        refs = []

        def _flatten(groups):
            for group in groups:
                if len(refs) >= limit:
                    return
                path = group.get("path") or group.get("name", "")
                if path:
                    refs.append(
                        self._group_ref(path, display_name=group.get("name"))
                    )
                _flatten(group.get("subGroups") or [])

        _flatten(tree)
        return refs

    def group_exists(self, external_id: str) -> bool:
        """Check group existence by full path."""
        result = self._admin_get(
            f"/group-by-path/{quote(external_id.lstrip('/'), safe='/')}"
        )
        if result is None:
            logging.info(
                "Keycloak group %r does not exist (share-time validation).",
                external_id,
            )
            return False
        return True
