# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Just-in-time provisioning of REANA users from the trusted issuer.

Users are looked up by their immutable IdP identity ``(iss, sub)``. On
first sight of an identity, the user is provisioned from the issuer's
userinfo response: either linked one-shot to a pre-existing unlinked
account with the same verified email (migration path), or created. The
required REANA role is enforced *before* any database write.
"""

import logging

from reana_db.database import Session
from reana_db.models import User
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from reana_server.auth.errors import ProvisioningError
from reana_server.auth.tokens import require_role
from reana_server.auth.userinfo import fetch_userinfo
from reana_server.config import REANA_AUTH


def verify_userinfo_subject(claims, userinfo):
    """Verify the userinfo ``sub`` matches the validated token ``sub``.

    UserInfo is fetched with the user's access token, but a confused-deputy
    or misconfigured issuer could return a response for a different subject;
    binding it to the token ``sub`` before any provisioning, linking, role
    check or group sync is a non-negotiable invariant
    (``auth_contract_freeze.md`` §2 and the provisioning contract).

    :raises ProvisioningError: when ``sub`` is missing or mismatched.
    """
    userinfo_sub = userinfo.get("sub")
    if not userinfo_sub:
        raise ProvisioningError("UserInfo response is missing 'sub'.")
    if userinfo_sub != claims.get("sub"):
        raise ProvisioningError(
            "UserInfo 'sub' does not match the access token 'sub'."
        )


def email_linking_allowed(iss, email, userinfo):
    """Return whether a new identity may be auto-linked to an account by email.

    Disabled by default; enabling it still requires a verified email and,
    when configured, the issuer and the email domain to be on their
    allow-lists (``auth_contract_freeze.md`` provisioning contract). An empty
    allow-list skips that particular check.
    """
    if not REANA_AUTH["email_linking_enabled"]:
        return False
    if userinfo.get("email_verified") is not True:
        return False
    issuer_allowlist = REANA_AUTH["email_linking_issuer_allowlist"]
    if issuer_allowlist and iss not in issuer_allowlist:
        return False
    domain_allowlist = REANA_AUTH["email_linking_domain_allowlist"]
    if domain_allowlist:
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if domain not in domain_allowlist:
            return False
    return True


def get_user_by_idp_identity(sub, iss):
    """Return the REANA user linked to the given IdP identity, if any."""
    return (
        Session.query(User)
        .filter_by(idp_subject=sub, idp_issuer=iss)
        .one_or_none()
    )


def _link_existing_user(user, sub, iss, userinfo):
    """One-shot link of an IdP identity to a pre-existing unlinked account."""
    if user.idp_subject is not None:
        raise ProvisioningError(
            f"Email '{user.email}' is already linked to a different "
            "identity. Please contact the administrators."
        )
    if userinfo.get("email_verified") is not True:
        # Linking by email is an account-takeover vector when the issuer
        # has not verified the address; fail closed and let administrators
        # resolve it (or the user verify their email at the issuer).
        raise ProvisioningError(
            f"Cannot link existing account '{user.email}': the issuer did "
            "not assert a verified email."
        )
    user.idp_issuer = iss
    user.idp_subject = sub
    if not user.full_name and userinfo.get("name"):
        user.full_name = userinfo["name"]
    if not user.username and userinfo.get("preferred_username"):
        user.username = userinfo["preferred_username"]
    logging.info(
        "Linked existing user %s to IdP identity (one-shot email match).",
        user.id_,
    )
    return user


def get_or_provision_user(claims, token, userinfo=None):
    """Return ``(user, is_new)`` for validated token claims, provisioning JIT.

    :param claims: validated JWT claims (``iss``/``sub`` guaranteed by
        :func:`reana_server.auth.tokens.validate_access_token`).
    :param token: the raw bearer token, used for the userinfo call on
        first sight of an identity when ``userinfo`` was not supplied.
    :param userinfo: optional already-fetched userinfo response, reused for
        role checks and group sync when EOSC entitlements live outside the JWT.
    :returns: ``(user, is_new)`` where ``is_new`` is ``True`` when the user
        was just provisioned (groups already synced); ``False`` for returning
        users (caller decides whether to re-sync).
    :raises MissingRoleError: when the user lacks the required REANA role.
    :raises ProvisioningError: when the user cannot be linked or created.
    """
    sub, iss = claims["sub"], claims["iss"]
    user = get_user_by_idp_identity(sub, iss)
    if user:
        if userinfo is not None:
            verify_userinfo_subject(claims, userinfo)
            require_role(claims, userinfo)
        return user, False

    # First sight of this identity: one userinfo round-trip, then link or
    # create. UserInfo is bound to the token subject, and the role gate runs
    # before any database write so that arbitrary issuer accounts cannot fill
    # the user table.
    userinfo = userinfo or fetch_userinfo(token)
    verify_userinfo_subject(claims, userinfo)
    require_role(claims, userinfo)
    email = userinfo["email"]
    try:
        existing = Session.query(User).filter_by(email=email).one_or_none()
        if existing is not None:
            if not email_linking_allowed(iss, email, userinfo):
                # Fail closed: a REANA account already uses this email but
                # automatic linking is disabled or not permitted for this
                # issuer/domain. An administrator must resolve it (or enable
                # linking via the allow-lists).
                raise ProvisioningError(
                    f"An account already exists for '{email}', but automatic "
                    "email linking is disabled or not permitted for this "
                    "issuer/domain. Please contact the administrators."
                )
            user = _link_existing_user(existing, sub, iss, userinfo)
        else:
            user = User(
                email=email,
                full_name=userinfo.get("name", email),
                username=userinfo.get("preferred_username", email),
                idp_issuer=iss,
                idp_subject=sub,
            )
            Session.add(user)
            logging.info("Provisioned new user for email %s (JIT).", email)
        Session.commit()
    except (IntegrityError, InvalidRequestError) as error:
        Session.rollback()
        raise ProvisioningError(f"Could not provision user: {error}")

    _sync_groups(user, userinfo)
    return user, True


def _sync_groups(user, userinfo):
    """Synchronize the user's group membership snapshot from userinfo.

    Sync failures must not fail authentication: the sync engine is
    fail-closed on malformed claims (memberships cleared), and transport
    problems leave the previous snapshot to age out via
    ``REANA_GROUP_MEMBERSHIP_MAX_AGE``.
    """
    try:
        from reana_server.groups.sync import sync_user_groups_from_userinfo

        sync_user_groups_from_userinfo(user, userinfo)
    except Exception:
        logging.exception(
            "Group membership sync failed for user %s.", user.id_
        )
