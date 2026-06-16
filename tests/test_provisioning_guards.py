# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Unit tests for the DB-free provisioning guards (contract invariants).

These cover the UserInfo ``sub`` binding and the email-linking policy without
the heavy app/DB fixture, so they run anywhere.
"""

import pytest

from reana_server.auth.errors import ProvisioningError
from reana_server.auth.provision import (
    email_linking_allowed,
    verify_userinfo_subject,
)
from reana_server.config import REANA_AUTH


class TestVerifyUserInfoSubject:
    def test_match_passes(self):
        verify_userinfo_subject({"sub": "s1"}, {"sub": "s1"})

    def test_missing_userinfo_sub_rejected(self):
        with pytest.raises(ProvisioningError):
            verify_userinfo_subject({"sub": "s1"}, {"email": "a@b.c"})

    def test_mismatched_sub_rejected(self):
        with pytest.raises(ProvisioningError):
            verify_userinfo_subject({"sub": "s1"}, {"sub": "s2"})


class TestEmailLinkingAllowed:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "email_linking_enabled", False)
        monkeypatch.setitem(REANA_AUTH, "email_linking_issuer_allowlist", [])
        monkeypatch.setitem(REANA_AUTH, "email_linking_domain_allowlist", [])

    def test_disabled_by_default(self):
        assert (
            email_linking_allowed("iss", "a@cern.ch", {"email_verified": True})
            is False
        )

    def test_enabled_verified_no_allowlists(self, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "email_linking_enabled", True)
        assert (
            email_linking_allowed("iss", "a@cern.ch", {"email_verified": True})
            is True
        )

    def test_enabled_requires_verified_email(self, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "email_linking_enabled", True)
        assert (
            email_linking_allowed("iss", "a@cern.ch", {"email_verified": False})
            is False
        )

    def test_issuer_allowlist_enforced(self, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "email_linking_enabled", True)
        monkeypatch.setitem(
            REANA_AUTH, "email_linking_issuer_allowlist", ["https://good"]
        )
        ui = {"email_verified": True}
        assert email_linking_allowed("https://good", "a@cern.ch", ui) is True
        assert email_linking_allowed("https://bad", "a@cern.ch", ui) is False

    def test_domain_allowlist_enforced(self, monkeypatch):
        monkeypatch.setitem(REANA_AUTH, "email_linking_enabled", True)
        monkeypatch.setitem(
            REANA_AUTH, "email_linking_domain_allowlist", ["cern.ch"]
        )
        ui = {"email_verified": True}
        assert email_linking_allowed("iss", "a@cern.ch", ui) is True
        assert email_linking_allowed("iss", "a@evil.com", ui) is False
