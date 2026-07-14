# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""INDIGO IAM group backend using the IAM SCIM API."""

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
    GroupClaimError,
    GroupRef,
)

DEFAULT_SCOPE = "scim:read"
DEFAULT_GROUPS_CLAIM = "groups"
DEFAULT_PAGE_SIZE = 100


class IndigoIamGroupBackend(GroupBackend):
    """Group backend backed by INDIGO IAM's SCIM API."""

    def __init__(self, config):
        """Initialize from one ``REANA_GROUP_BACKENDS`` entry.

        Expected keys: ``provider`` (default ``indigo_iam``), ``base_url`` or
        ``scim_base_url``, ``client_id`` and ``client_secret_env``. The backend
        reads login-time memberships from the configured userinfo group claim
        when present, and uses SCIM for live refresh, group search and group
        existence checks.
        """
        self.provider = config.get("provider", "indigo_iam")
        self.base_url = config.get("base_url", "").rstrip("/")
        self.scim_base_url = (
            config.get("scim_base_url")
            or (self.base_url.rstrip("/") + "/scim" if self.base_url else "")
        ).rstrip("/")
        self.token_url = config.get("token_url") or (
            self.base_url.rstrip("/") + "/token" if self.base_url else ""
        )
        self.client_id = config.get("client_id", "")
        secret_env = config.get(
            "client_secret_env",
            f"REANA_GROUP_BACKEND_{self.provider.upper()}_CLIENT_SECRET",
        )
        self.client_secret = os.getenv(secret_env, "")
        self.scope = config.get("scope", DEFAULT_SCOPE)
        self.groups_claim = config.get("groups_claim", DEFAULT_GROUPS_CLAIM)
        self.ca_bundle = config.get("ca_bundle", "")
        self.tls_verify = str(config.get("tls_verify", "true")).lower() not in (
            "0",
            "false",
            "no",
        )
        self.page_size = int(config.get("page_size") or DEFAULT_PAGE_SIZE)
        self.http_timeout = config.get("http_timeout", 10)
        self._token_lock = threading.Lock()
        self._token = None
        self._token_expires_at = 0.0

    @property
    def _verify(self):
        return False  # self.ca_bundle or self.tls_verify

    @property
    def scim_enabled(self) -> bool:
        """Whether SCIM client credentials are configured."""
        return bool(
            self.scim_base_url
            and self.token_url
            and self.client_id
            and self.client_secret
        )

    def _require_scim(self):
        if not self.scim_enabled:
            raise GroupBackendError(
                "INDIGO IAM SCIM client is not configured "
                f"(provider {self.provider!r}); set base_url/scim_base_url, "
                "client_id and the secret in client_secret_env."
            )

    def _get_access_token(self):
        with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            try:
                response = requests.post(
                    self.token_url,
                    data={
                        "grant_type": "client_credentials",
                        "scope": self.scope,
                    },
                    auth=(self.client_id, self.client_secret),
                    headers={"Accept": "application/json"},
                    timeout=self.http_timeout,
                    verify=self._verify,
                )
                response.raise_for_status()
                token_data = response.json()
                token = token_data["access_token"]
            except (requests.RequestException, ValueError, KeyError) as error:
                raise GroupBackendError(f"Could not obtain INDIGO IAM token: {error}")
            self._token = token
            self._token_expires_at = (
                time.monotonic() + token_data.get("expires_in", 60) - 30
            )
            return self._token

    def _scim_get(self, path, params=None, ok_statuses=(200,)):
        self._require_scim()
        url = self.scim_base_url.rstrip("/") + "/" + path.lstrip("/")
        try:
            response = requests.get(
                url,
                params=params,
                headers={
                    "Accept": "application/scim+json, application/json",
                    "Authorization": f"Bearer {self._get_access_token()}",
                },
                timeout=self.http_timeout,
                verify=self._verify,
            )
        except requests.RequestException as error:
            raise GroupBackendError(f"INDIGO IAM SCIM request failed: {error}")
        if response.status_code == 404:
            return None
        if response.status_code not in ok_statuses:
            raise GroupBackendError(
                f"INDIGO IAM SCIM returned {response.status_code} for {path}."
            )
        try:
            return response.json()
        except ValueError as error:
            raise GroupBackendError(f"INDIGO IAM SCIM returned invalid JSON: {error}")

    @staticmethod
    def _resources(payload):
        if not isinstance(payload, dict):
            return []
        resources = payload.get("Resources", [])
        return resources if isinstance(resources, list) else []

    def _group_ref(self, external_id, display_name=None) -> Optional[GroupRef]:
        external_id = (external_id or "").strip()
        if not external_id:
            return None
        display_name = (display_name or external_id).strip()
        path = display_name if display_name.startswith("/") else f"/{display_name}"
        return GroupRef(
            provider=self.provider,
            external_id=external_id,
            display_name=display_name,
            path=path,
        )

    def _group_ref_from_claim_value(self, value) -> Optional[GroupRef]:
        if isinstance(value, dict):
            return self._group_ref(
                value.get("value") or value.get("id"),
                value.get("display") or value.get("displayName"),
            )
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            return self._group_ref(value, value.lstrip("/"))
        return None

    def _group_ref_from_scim_group(self, group) -> Optional[GroupRef]:
        if not isinstance(group, dict):
            return None
        return self._group_ref(group.get("id"), group.get("displayName"))

    def _group_refs_from_user(self, user_doc) -> List[GroupRef]:
        if not isinstance(user_doc, dict):
            raise GroupBackendError("INDIGO IAM SCIM user response is invalid.")
        raw_groups = user_doc.get("groups", [])
        if raw_groups is None:
            raw_groups = []
        if not isinstance(raw_groups, list):
            raise GroupBackendError("INDIGO IAM SCIM user 'groups' is not a list.")
        refs = []
        for raw_group in raw_groups:
            ref = self._group_ref_from_claim_value(raw_group)
            if ref is not None:
                refs.append(ref)
        return refs

    def extract_memberships(self, userinfo: dict) -> List[GroupRef]:
        """Parse memberships from the configured userinfo group claim."""
        if self.groups_claim not in userinfo:
            raise GroupClaimError(
                f"Userinfo response has no '{self.groups_claim}' claim."
            )
        raw_groups = userinfo[self.groups_claim]
        if not isinstance(raw_groups, list):
            raise GroupClaimError(
                f"Userinfo claim '{self.groups_claim}' is not a list."
            )
        refs = []
        for raw_group in raw_groups:
            ref = self._group_ref_from_claim_value(raw_group)
            if ref is not None:
                refs.append(ref)
        return refs

    def extract_memberships_for_user(self, user, userinfo: dict) -> List[GroupRef]:
        """Prefer SCIM lookup after provisioning so group ids stay stable."""
        if self.scim_enabled and user.idp_subject:
            return self.fetch_memberships(user)
        return self.extract_memberships(userinfo)

    def fetch_memberships(self, user) -> List[GroupRef]:
        """Fetch a user's SCIM groups by OIDC subject/SCIM user id."""
        if not user.idp_subject:
            raise GroupBackendError(f"User {user.id_} has no linked IdP identity.")
        user_doc = self._scim_get(f"Users/{quote(user.idp_subject, safe='')}")
        if user_doc is None:
            raise GroupBackendError(
                f"INDIGO IAM SCIM user {user.idp_subject!r} not found."
            )
        return self._group_refs_from_user(user_doc)

    def search_groups(self, query: str, limit: int = 20) -> List[GroupRef]:
        """Search IAM groups by display name."""
        payload = self._scim_get(
            "Groups",
            params={
                "filter": f'displayName co "{query}"',
                "startIndex": 1,
                "count": min(limit, self.page_size),
            },
        )
        refs = []
        for group in self._resources(payload)[:limit]:
            ref = self._group_ref_from_scim_group(group)
            if ref is not None:
                refs.append(ref)
        return refs

    def group_exists(self, external_id: str) -> bool:
        """Validate a group by stable SCIM group id."""
        result = self._scim_get(f"Groups/{quote(external_id, safe='')}")
        if result is None:
            logging.info(
                "INDIGO IAM group %r does not exist (share-time validation).",
                external_id,
            )
            return False
        return True
