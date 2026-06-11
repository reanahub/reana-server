# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Tests for the group backend abstraction and sync engine."""

from unittest.mock import Mock, patch

import pytest
from reana_db.models import ExternalGroup, UserGroupMembership

import reana_server.groups.sync as sync_module
from reana_server.groups.base import (
    GroupBackend,
    GroupClaimError,
    GroupRef,
)
from reana_server.groups.keycloak import KeycloakGroupBackend
from reana_server.groups.sync import (
    _normalize_refs,
    clear_user_groups,
    sync_user_groups,
    sync_user_groups_from_userinfo,
)


def _refs(provider, *external_ids):
    return [
        GroupRef(
            provider=provider,
            external_id=external_id,
            display_name=external_id.rsplit("/", 1)[-1],
        )
        for external_id in external_ids
    ]


def _memberships(session, user):
    return {
        group.external_id: membership
        for membership, group in (
            session.query(UserGroupMembership, ExternalGroup)
            .filter(
                UserGroupMembership.group_id == ExternalGroup.id_,
                UserGroupMembership.user_id == user.id_,
            )
            .all()
        )
    }


class TestNormalization:
    """Validation, deduplication and capping of backend output."""

    def test_drops_invalid_identifiers(self):
        refs = _refs("keycloak", "/local/atlas") + [
            GroupRef("keycloak", "bad\nnewline", "x"),
            GroupRef("keycloak", "", "x"),
            GroupRef("keycloak", " ", "x"),
        ]
        normalized = _normalize_refs(refs, "keycloak")
        assert [r.external_id for r in normalized] == ["/local/atlas"]

    def test_drops_foreign_provider(self):
        refs = _refs("keycloak", "/local/atlas") + _refs("cern", "atlas")
        normalized = _normalize_refs(refs, "keycloak")
        assert [r.external_id for r in normalized] == ["/local/atlas"]

    def test_deduplicates_and_sorts(self):
        refs = _refs("keycloak", "/b", "/a", "/b")
        normalized = _normalize_refs(refs, "keycloak")
        assert [r.external_id for r in normalized] == ["/a", "/b"]

    def test_caps_group_count(self, monkeypatch):
        monkeypatch.setattr(sync_module, "MAX_GROUPS_PER_SYNC", 2)
        refs = _refs("keycloak", "/a", "/b", "/c")
        normalized = _normalize_refs(refs, "keycloak")
        assert len(normalized) == 2


class TestSyncEngine:
    """Diff-based snapshot updates owned by the sync engine."""

    def test_initial_sync_and_diff(self, app, session, default_user):
        sync_user_groups(
            default_user, "keycloak", _refs("keycloak", "/a", "/b")
        )
        first = _memberships(session, default_user)
        assert set(first) == {"/a", "/b"}

        sync_user_groups(
            default_user, "keycloak", _refs("keycloak", "/b", "/c")
        )
        second = _memberships(session, default_user)
        assert set(second) == {"/b", "/c"}
        # The kept membership rows got a fresh synced_at.
        assert second["/b"].synced_at >= first["/b"].synced_at

    def test_provider_isolation(self, app, session, default_user):
        sync_user_groups(default_user, "keycloak", _refs("keycloak", "/a"))
        sync_user_groups(default_user, "cern", _refs("cern", "atlas"))
        # Re-syncing one provider never touches the other's snapshot.
        sync_user_groups(default_user, "keycloak", [])
        memberships = _memberships(session, default_user)
        assert set(memberships) == {"atlas"}

    def test_group_upsert_updates_display_name(
        self, app, session, default_user
    ):
        sync_user_groups(default_user, "keycloak", _refs("keycloak", "/a"))
        sync_user_groups(
            default_user,
            "keycloak",
            [GroupRef("keycloak", "/a", "renamed")],
        )
        group = (
            session.query(ExternalGroup)
            .filter_by(provider="keycloak", external_id="/a")
            .one()
        )
        assert group.display_name == "renamed"

    def test_fail_closed_on_malformed_claim(
        self, app, session, default_user
    ):
        class FailingBackend(GroupBackend):
            provider = "keycloak"

            def extract_memberships(self, userinfo):
                raise GroupClaimError("claim absent")

            def fetch_memberships(self, user):
                return []

            def search_groups(self, query, limit=20):
                return []

            def group_exists(self, external_id):
                return False

        sync_user_groups(default_user, "keycloak", _refs("keycloak", "/a"))
        with patch.object(
            sync_module,
            "get_group_backends",
            return_value={"keycloak": FailingBackend()},
        ):
            sync_user_groups_from_userinfo(default_user, {})
        assert _memberships(session, default_user) == {}

    def test_clear_user_groups(self, app, session, default_user):
        sync_user_groups(default_user, "keycloak", _refs("keycloak", "/a"))
        clear_user_groups(default_user, "keycloak")
        assert _memberships(session, default_user) == {}


class TestKeycloakBackend:
    """Claim parsing and Admin API access of the Keycloak backend."""

    @pytest.fixture
    def backend(self):
        return KeycloakGroupBackend(
            {
                "provider": "keycloak",
                "server_url": "https://auth.example.org",
                "realm": "reana",
                "client_id": "reana-server-internal",
            }
        )

    def test_extract_memberships(self, backend):
        refs = backend.extract_memberships(
            {"groups": ["/local/atlas", "/local/cms/readers", 42]}
        )
        assert [(r.external_id, r.display_name) for r in refs] == [
            ("/local/atlas", "atlas"),
            ("/local/cms/readers", "readers"),
        ]
        assert all(r.provider == "keycloak" for r in refs)

    def test_extract_memberships_missing_claim(self, backend):
        with pytest.raises(GroupClaimError):
            backend.extract_memberships({"email": "x@example.org"})

    def test_extract_memberships_malformed_claim(self, backend):
        with pytest.raises(GroupClaimError):
            backend.extract_memberships({"groups": "not-a-list"})

    def test_search_groups_flattens_subgroups(self, backend):
        tree = [
            {
                "name": "local",
                "path": "/local",
                "subGroups": [
                    {"name": "atlas", "path": "/local/atlas", "subGroups": []}
                ],
            }
        ]
        with patch.object(backend, "_admin_get", return_value=tree):
            refs = backend.search_groups("loc")
        assert [r.external_id for r in refs] == ["/local", "/local/atlas"]

    def test_search_groups_respects_limit(self, backend):
        tree = [
            {"name": f"g{i}", "path": f"/g{i}", "subGroups": []}
            for i in range(30)
        ]
        with patch.object(backend, "_admin_get", return_value=tree):
            refs = backend.search_groups("g", limit=5)
        assert len(refs) == 5

    def test_group_exists(self, backend):
        with patch.object(
            backend, "_admin_get", return_value={"id": "abc"}
        ):
            assert backend.group_exists("/local/atlas") is True
        with patch.object(backend, "_admin_get", return_value=None):
            assert backend.group_exists("/local/nope") is False

    def test_fetch_memberships_paginates(self, backend):
        user = Mock(idp_subject="kc-user-id", id_="user-id")
        page_one = [
            {"name": f"g{i}", "path": f"/g{i}"} for i in range(100)
        ]
        page_two = [{"name": "last", "path": "/last"}]
        with patch.object(
            backend, "_admin_get", side_effect=[page_one, page_two]
        ) as mocked:
            refs = backend.fetch_memberships(user)
        assert len(refs) == 101
        assert mocked.call_count == 2
