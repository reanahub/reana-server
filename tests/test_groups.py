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


KC_GROUP_A = "11111111-1111-4111-8111-111111111111"
KC_GROUP_B = "22222222-2222-4222-8222-222222222222"
KC_GROUP_C = "33333333-3333-4333-8333-333333333333"
KC_GROUP_LOCAL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
KC_GROUP_ATLAS = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
KC_GROUP_LAST = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _refs(provider, *external_ids):
    return [
        GroupRef(
            provider=provider,
            external_id=external_id,
            display_name=external_id,
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
        refs = _refs("keycloak", KC_GROUP_A) + [
            GroupRef("keycloak", "bad\nnewline", "x"),
            GroupRef("keycloak", "", "x"),
            GroupRef("keycloak", " ", "x"),
        ]
        normalized = _normalize_refs(refs, "keycloak")
        assert [r.external_id for r in normalized] == [KC_GROUP_A]

    def test_drops_foreign_provider(self):
        refs = _refs("keycloak", KC_GROUP_A) + _refs("cern", "atlas")
        normalized = _normalize_refs(refs, "keycloak")
        assert [r.external_id for r in normalized] == [KC_GROUP_A]

    def test_deduplicates_and_sorts(self):
        refs = _refs("keycloak", KC_GROUP_B, KC_GROUP_A, KC_GROUP_B)
        normalized = _normalize_refs(refs, "keycloak")
        assert [r.external_id for r in normalized] == [KC_GROUP_A, KC_GROUP_B]

    def test_caps_group_count(self, monkeypatch):
        monkeypatch.setattr(sync_module, "MAX_GROUPS_PER_SYNC", 2)
        refs = _refs("keycloak", KC_GROUP_A, KC_GROUP_B, KC_GROUP_C)
        normalized = _normalize_refs(refs, "keycloak")
        assert len(normalized) == 2


class TestSyncEngine:
    """Diff-based snapshot updates owned by the sync engine."""

    def test_initial_sync_and_diff(self, app, session, user0):
        sync_user_groups(
            user0, "keycloak", _refs("keycloak", KC_GROUP_A, KC_GROUP_B)
        )
        first = _memberships(session, user0)
        assert set(first) == {KC_GROUP_A, KC_GROUP_B}

        sync_user_groups(
            user0, "keycloak", _refs("keycloak", KC_GROUP_B, KC_GROUP_C)
        )
        second = _memberships(session, user0)
        assert set(second) == {KC_GROUP_B, KC_GROUP_C}
        # The kept membership rows got a fresh synced_at.
        assert second[KC_GROUP_B].synced_at >= first[KC_GROUP_B].synced_at

    def test_provider_isolation(self, app, session, user0):
        sync_user_groups(user0, "keycloak", _refs("keycloak", KC_GROUP_A))
        sync_user_groups(user0, "cern", _refs("cern", "atlas"))
        # Re-syncing one provider never touches the other's snapshot.
        sync_user_groups(user0, "keycloak", [])
        memberships = _memberships(session, user0)
        assert set(memberships) == {"atlas"}

    def test_group_upsert_updates_display_name(
        self, app, session, user0
    ):
        sync_user_groups(user0, "keycloak", _refs("keycloak", KC_GROUP_A))
        sync_user_groups(
            user0,
            "keycloak",
            [GroupRef("keycloak", KC_GROUP_A, "renamed", "/renamed/path")],
        )
        group = (
            session.query(ExternalGroup)
            .filter_by(provider="keycloak", external_id=KC_GROUP_A)
            .one()
        )
        assert group.display_name == "renamed"

    def test_fail_closed_on_malformed_claim(
        self, app, session, user0
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

        sync_user_groups(user0, "keycloak", _refs("keycloak", KC_GROUP_A))
        with patch.object(
            sync_module,
            "get_group_backends",
            return_value={"keycloak": FailingBackend()},
        ):
            sync_user_groups_from_userinfo(user0, {})
        assert _memberships(session, user0) == {}

    def test_clear_user_groups(self, app, session, user0):
        sync_user_groups(user0, "keycloak", _refs("keycloak", KC_GROUP_A))
        clear_user_groups(user0, "keycloak")
        assert _memberships(session, user0) == {}


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
        userinfo = {
            "groups": [
                "/local/atlas",
                {
                    "id": KC_GROUP_B,
                    "name": "readers",
                    "path": "/local/cms/readers",
                },
                42,
            ]
        }
        with patch.object(
            backend,
            "_admin_get",
            return_value={
                "id": KC_GROUP_A,
                "name": "atlas",
                "path": "/local/atlas",
            },
        ):
            refs = backend.extract_memberships(userinfo)
        assert [(r.external_id, r.display_name, r.path) for r in refs] == [
            (KC_GROUP_A, "atlas", "/local/atlas"),
            (KC_GROUP_B, "readers", "/local/cms/readers"),
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
                "id": KC_GROUP_LOCAL,
                "name": "local",
                "path": "/local",
                "subGroups": [
                    {
                        "id": KC_GROUP_ATLAS,
                        "name": "atlas",
                        "path": "/local/atlas",
                        "subGroups": [],
                    }
                ],
            }
        ]
        with patch.object(backend, "_admin_get", return_value=tree):
            refs = backend.search_groups("loc")
        assert [r.external_id for r in refs] == [KC_GROUP_LOCAL, KC_GROUP_ATLAS]
        assert [r.path for r in refs] == ["/local", "/local/atlas"]

    def test_search_groups_respects_limit(self, backend):
        tree = [
            {
                "id": f"00000000-0000-4000-8000-{i:012d}",
                "name": f"g{i}",
                "path": f"/g{i}",
                "subGroups": [],
            }
            for i in range(30)
        ]
        with patch.object(backend, "_admin_get", return_value=tree):
            refs = backend.search_groups("g", limit=5)
        assert len(refs) == 5

    def test_group_exists(self, backend):
        with patch.object(
            backend, "_admin_get", return_value={"id": "abc"}
        ) as mocked:
            assert backend.group_exists(KC_GROUP_A) is True
        mocked.assert_called_with(f"/groups/{KC_GROUP_A}")
        with patch.object(backend, "_admin_get", return_value=None):
            assert backend.group_exists(KC_GROUP_B) is False

    def test_fetch_memberships_paginates(self, backend):
        user = Mock(idp_subject="kc-user-id", id_="user-id")
        page_one = [
            {
                "id": f"00000000-0000-4000-8000-{i:012d}",
                "name": f"g{i}",
                "path": f"/g{i}",
            }
            for i in range(100)
        ]
        page_two = [{"id": KC_GROUP_LAST, "name": "last", "path": "/last"}]
        with patch.object(
            backend, "_admin_get", side_effect=[page_one, page_two]
        ) as mocked:
            refs = backend.fetch_memberships(user)
        assert len(refs) == 101
        assert mocked.call_count == 2
