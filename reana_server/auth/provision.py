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

_MAX_CLAIM_LENGTH = 255
"""Matches the ``String(255)`` column length shared by every identity/profile
column this module writes to (``User.email``/``full_name``/``username``/
``idp_issuer``/``idp_subject`` -- see ``reana_db.models.User``). An oversized
issuer-supplied claim that reached ``Session.commit()`` unvalidated used to
surface as an unhandled ``sqlalchemy.exc.DataError`` (neither an
``IntegrityError`` nor an ``InvalidRequestError``, so uncaught by either
existing handler below) -- an unhandled 500 on first login instead of the
usual, uniformly-handled :class:`ProvisioningError`.
"""


def _sanitize_claim(value):
    """Strip control characters and bound the length of a presentation-only claim.

    ``email``, ``name`` and ``preferred_username`` come verbatim from the
    issuer's userinfo response and reach ``logging`` calls and (via
    ``reana-admin export-users``) a CSV writer. Without stripping, a user who
    controls their own IdP profile could embed CR/LF to forge adjacent log
    lines, or an ANSI escape sequence (which always starts with the ESC
    byte, also stripped here) to corrupt an admin's terminal.

    ``name`` and ``preferred_username`` are presentation-only, so truncating
    a pathologically long value to the storable length is safe here --
    unlike an identity key (see :func:`_validated_email`,
    :func:`_validated_identity_claim`), where truncation could silently
    collide two different identities onto the same stored value.

    A non-string value (possible only via the ``userinfo=`` parameter that
    exists for tests to inject a raw dict bypassing :func:`fetch_userinfo`'s
    own validation -- every production call site goes through that
    function, which already guarantees ``str`` or absent) is treated the
    same as an absent one: ``None``, not a crash. Matches
    :func:`_validated_identity_claim`'s reasoning for why this must be
    checked at all, without escalating to a hard reject the way that
    function does for an identity key -- this is presentation-only.
    """
    if value is None or not isinstance(value, str):
        return None
    return _CONTROL_CHAR_RE.sub("", value)[:_MAX_CLAIM_LENGTH]


def _validated_email(userinfo):
    """Return an identity email only when it cannot normalize to another key."""
    email = userinfo.get("email")
    if not isinstance(email, str) or not email:
        raise ProvisioningError("UserInfo email is missing or not a string.")
    if _CONTROL_CHAR_RE.search(email):
        raise ProvisioningError("UserInfo email contains forbidden control characters.")
    if len(email) > _MAX_CLAIM_LENGTH:
        raise ProvisioningError("UserInfo email exceeds the maximum allowed length.")
    return email


def _validated_identity_claim(value, claim_name):
    """Reject an issuer/subject claim that is not a storable non-empty string.

    Unlike a presentation-only field, silently truncating an identity key
    risks two different real identities colliding onto the same stored
    value, so this rejects rather than truncates -- matching
    :func:`_validated_email`'s treatment of the other identity key.

    ``iss`` is pinned to an exact configured string by JWT validation
    (``tokens.py``'s ``"iss": {"value": ...}`` claim option), but ``sub`` is
    only required to be *present*, not string-typed -- a token with e.g. a
    numeric ``sub`` would otherwise reach ``len(value)`` below and raise an
    uncaught ``TypeError`` instead of the controlled error every other
    malformed-claim case in this module produces.
    """
    if not isinstance(value, str) or not value:
        raise ProvisioningError(f"Token {claim_name} is missing or not a string.")
    if len(value) > _MAX_CLAIM_LENGTH:
        raise ProvisioningError(
            f"Token {claim_name} exceeds the maximum allowed length."
        )
    return value


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

    A small number of institutional issuers never emit the standard OIDC
    ``email_verified`` claim at all, even though their email is verified
    out-of-band by the issuer itself (no self-service "add any email" step
    exists there). ``email_linking_assume_verified_issuers`` lets an
    administrator explicitly attest to that for one issuer at a time,
    without weakening the check for every other issuer.

    That attestation only covers an *absent* claim. An issuer that
    explicitly asserts ``email_verified: false`` for a specific address is
    saying something concrete about that address, not merely omitting the
    claim the way the assume-verified issuers are attested to -- it must
    never be treated the same as an absent claim, trusted issuer or not.
    """
    auth_config = get_auth_config()
    if not auth_config["email_linking_enabled"]:
        return False
    assume_verified_issuers = auth_config["email_linking_assume_verified_issuers"]
    if "email_verified" in userinfo:
        # The administrator attestation covers only issuers that omit the
        # claim. Any present value must be the exact JSON boolean ``true``;
        # accepting null, 0, "false", or another malformed value would turn
        # the fallback back into trust for an explicitly unverified claim.
        if userinfo["email_verified"] is not True:
            return False
    elif iss not in assume_verified_issuers:
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
    """One-shot link of an IdP identity to a pre-existing unlinked account.

    Callers must have already confirmed ``email_linking_allowed(iss, email,
    userinfo)`` for this identity -- that is the single source of truth for
    whether the issuer's email assertion (verified claim, or an explicit
    per-issuer administrator attestation) is trustworthy enough to link on.
    Duplicating that condition here previously drifted out of sync with it.
    """
    if user.idp_subject is not None:
        raise ProvisioningError(
            f"Email '{user.email}' is already linked to a different "
            "identity. Please contact the administrators."
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
    _validated_identity_claim(sub, "subject")
    _validated_identity_claim(iss, "issuer")
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
    email = _validated_email(userinfo)
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
