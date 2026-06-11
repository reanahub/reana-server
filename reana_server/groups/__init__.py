# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server group backends.

A *group backend* connects REANA to one provider of group information
(local Keycloak groups first; CERN Authorization Service, INDIGO IAM, LDAP
later). Backends only ever produce normalized
:class:`reana_server.groups.base.GroupRef` values; all database writes are
owned by the single sync engine in :mod:`reana_server.groups.sync`, and all
authorization decisions read the reana-db snapshot — never a backend.

Backends are configured through the ``REANA_GROUP_BACKENDS`` environment
variable (JSON list, see ``reana_server.config``); adding a new backend
type means adding one module here and one entry in ``_BACKEND_TYPES`` —
call sites iterate the registry and need no changes.
"""

import logging
from typing import Dict, Optional

from reana_server.config import REANA_GROUP_BACKENDS
from reana_server.groups.base import GroupBackend
from reana_server.groups.keycloak import KeycloakGroupBackend

_BACKEND_TYPES = {
    "keycloak": KeycloakGroupBackend,
}

_registry: Optional[Dict[str, GroupBackend]] = None


def get_group_backends() -> Dict[str, GroupBackend]:
    """Return the configured group backends, keyed by provider tag."""
    global _registry
    if _registry is None:
        registry = {}
        for backend_config in REANA_GROUP_BACKENDS:
            backend_type = backend_config.get("type")
            backend_class = _BACKEND_TYPES.get(backend_type)
            if not backend_class:
                logging.error(
                    "Unknown group backend type %r, ignoring.", backend_type
                )
                continue
            backend = backend_class(backend_config)
            if backend.provider in registry:
                logging.error(
                    "Duplicate group backend provider %r, ignoring.",
                    backend.provider,
                )
                continue
            registry[backend.provider] = backend
        _registry = registry
    return _registry


def get_group_backend(provider: str) -> Optional[GroupBackend]:
    """Return the backend for a provider tag, or ``None``."""
    return get_group_backends().get(provider)
