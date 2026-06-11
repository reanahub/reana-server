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


def get_or_provision_user(claims, token):
    """Return the REANA user for validated token claims, provisioning JIT.

    :param claims: validated JWT claims (``iss``/``sub`` guaranteed by
        :func:`reana_server.auth.tokens.validate_access_token`).
    :param token: the raw bearer token, used for the userinfo call on
        first sight of an identity.
    :raises MissingRoleError: when the user lacks the required REANA role.
    :raises ProvisioningError: when the user cannot be linked or created.
    """
    sub, iss = claims["sub"], claims["iss"]
    user = get_user_by_idp_identity(sub, iss)
    if user:
        return user

    # First sight of this identity: one userinfo round-trip, then link or
    # create. The role gate runs before any database write so that
    # arbitrary issuer accounts cannot fill the user table.
    userinfo = fetch_userinfo(token)
    require_role(claims, userinfo)
    email = userinfo["email"]
    try:
        existing = Session.query(User).filter_by(email=email).one_or_none()
        if existing is not None:
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
    return user


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
