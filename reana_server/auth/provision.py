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
required REANA role is read from the validated access token and enforced
*before* UserInfo I/O or any database write.
"""

import logging
import re

from reana_db.database import Session
from reana_db.models import User
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from reana_server.auth.errors import ProvisioningError
from reana_server.auth.config import get_auth_config
from reana_server.auth.tokens import require_role
from reana_server.auth.userinfo import fetch_userinfo

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_claim(value):
    """Strip control characters from an untrusted issuer-supplied claim.

    ``email``, ``name`` and ``preferred_username`` come verbatim from the
    issuer's userinfo response and reach ``logging`` calls and (via
    ``reana-admin export-users``) a CSV writer. Without this, a user who
    controls their own IdP profile could embed CR/LF to forge adjacent log
    lines, or an ANSI escape sequence (which always starts with the ESC
    byte, also stripped here) to corrupt an admin's terminal.
    """
    if value is None:
        return value
    return _CONTROL_CHAR_RE.sub("", value)


def verify_userinfo_subject(claims, userinfo):
    """Verify the userinfo ``sub`` matches the validated token ``sub``.

    UserInfo is fetched with the user's access token, but a confused-deputy
    or misconfigured issuer could return a response for a different subject;
    binding it to the token ``sub`` before provisioning, linking or role
    checks prevents identity confusion.

    :raises ProvisioningError: when ``sub`` is missing or mismatched.
    """
    userinfo_sub = userinfo.get("sub")
    if not userinfo_sub:
        raise ProvisioningError("UserInfo response is missing 'sub'.")
    if userinfo_sub != claims.get("sub"):
        raise ProvisioningError("UserInfo 'sub' does not match the access token 'sub'.")


def email_linking_allowed(iss, email, userinfo):
    """Return whether a new identity may be auto-linked to an account by email.

    Disabled by default; enabling it still requires a verified email and,
    when configured, the issuer and the email domain to be on their
    allow-lists. An empty allow-list skips that particular check.
    """
    auth_config = get_auth_config()
    if not auth_config["email_linking_enabled"]:
        return False
    if userinfo.get("email_verified") is not True:
        return False
    issuer_allowlist = auth_config["email_linking_issuer_allowlist"]
    if issuer_allowlist and iss not in issuer_allowlist:
        return False
    domain_allowlist = auth_config["email_linking_domain_allowlist"]
    if domain_allowlist:
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if domain not in domain_allowlist:
            return False
    return True


def get_user_by_idp_identity(sub, iss):
    """Return the REANA user linked to the given IdP identity, if any."""
    return Session.query(User).filter_by(idp_subject=sub, idp_issuer=iss).one_or_none()


def link_user_identity(user, iss, sub):
    """Explicitly link one REANA user to one immutable OIDC identity.

    The operation is idempotent for an identical existing link and refuses
    to move either side of an existing link. The caller owns the transaction.
    """
    if not iss or not sub:
        raise ProvisioningError("OIDC issuer and subject must both be provided.")
    identity_owner = get_user_by_idp_identity(sub, iss)
    if identity_owner and identity_owner.id_ != user.id_:
        raise ProvisioningError("OIDC identity is already linked to another user.")
    if user.idp_issuer or user.idp_subject:
        if user.idp_issuer == iss and user.idp_subject == sub:
            return user
        raise ProvisioningError("User is already linked to a different OIDC identity.")
    user.idp_issuer = iss
    user.idp_subject = sub
    return user


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
    link_user_identity(user, iss, sub)
    if not user.full_name and userinfo.get("name"):
        user.full_name = _sanitize_claim(userinfo["name"])
    if not user.username and userinfo.get("preferred_username"):
        user.username = _sanitize_claim(userinfo["preferred_username"])
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
    :param userinfo: optional already-fetched UserInfo response used only for
        identity provisioning/linking. It is never an authorization source.
    :returns: ``(user, is_new)`` where ``is_new`` is ``True`` when the user
        was just provisioned; ``False`` for returning users.
    :raises MissingRoleError: when the user lacks the required REANA role.
    :raises ProvisioningError: when the user cannot be linked or created.
    :raises IntegrityError: for database integrity failures that do not leave
        a concurrently committed user with the same immutable IdP identity.
    """
    sub, iss = claims["sub"], claims["iss"]
    require_role(claims)
    user = get_user_by_idp_identity(sub, iss)
    if user:
        if userinfo is not None:
            verify_userinfo_subject(claims, userinfo)
        return user, False

    # First sight of this identity: one userinfo round-trip, then link or
    # create. UserInfo is bound to the token subject and supplies identity
    # profile data only; it cannot grant REANA access.
    userinfo = userinfo or fetch_userinfo(token)
    verify_userinfo_subject(claims, userinfo)
    email = _sanitize_claim(userinfo["email"])
    try:
        existing = Session.query(User).filter_by(email=email).one_or_none()
        if existing is not None:
            if existing.idp_issuer == iss and existing.idp_subject == sub:
                # A concurrent JIT request provisioned this exact identity
                # between the identity lookup above and this email lookup. The
                # match is the same immutable ``(iss, sub)`` identity, not a
                # foreign account, so reuse it instead of attempting to link
                # (which would raise and surface as a spurious first-login 403).
                logging.info(
                    "Reused concurrently provisioned IdP identity for user %s.",
                    existing.id_,
                )
                return existing, False
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
                full_name=_sanitize_claim(userinfo.get("name", email)),
                username=_sanitize_claim(userinfo.get("preferred_username", email)),
                idp_issuer=iss,
                idp_subject=sub,
            )
            Session.add(user)
            logging.info("Provisioned new user for email %s (JIT).", email)
        Session.commit()
    except IntegrityError:
        Session.rollback()
        # Any uniqueness constraint can surface first when concurrent JIT
        # transactions create the same identity (for example email or primary
        # key before the identity pair). The immutable identity lookup, not
        # the backend-specific constraint name, decides whether this failure
        # is the losing half of that race.
        winner = get_user_by_idp_identity(sub, iss)
        if winner is not None:
            logging.info(
                "Reused concurrently provisioned IdP identity for user %s.",
                winner.id_,
            )
            return winner, False
        raise ProvisioningError(
            "Could not provision user because the identity could not be linked safely."
        )
    except InvalidRequestError as error:
        Session.rollback()
        raise ProvisioningError(f"Could not provision user: {error}")

    return user, True
