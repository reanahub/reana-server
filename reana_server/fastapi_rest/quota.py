# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Quota management endpoints (authenticated by a management secret).

These are administrative/service endpoints (not user-facing): the caller must
present ``X-Quota-Management-Secret``. The user-facing quota is part of the
``/api/you`` response.
"""

import secrets as _secrets
from typing import Optional

from fastapi import APIRouter, Body, Header, Query
from fastapi.responses import JSONResponse
from marshmallow import Schema, ValidationError, fields
from reana_db.database import Session
from reana_db.models import ResourceType, UserResource
from reana_db.utils import get_default_quota_resource

from reana_server.config import REANA_QUOTA_MANAGEMENT_SECRET
from reana_server.utils import (
    _get_users,
    _set_quota_limit,
    _set_quota_period,
    serialize_utc_datetime,
)

router = APIRouter(tags=["quota"])


class SetQuotaLimitBodySchema(Schema):
    """Schema for the set_quota_limit endpoint body."""

    user_id = fields.Str()
    email = fields.Str()
    resource_type = fields.Str(required=True)
    limit = fields.Int(required=True)


class PatchQuotaBodySchema(Schema):
    """Schema for the patch_quota endpoint body."""

    user_id = fields.Str()
    email = fields.Str()
    resource_type = fields.Str(required=True)
    quota_period_months = fields.Int(allow_none=True, strict=True)
    quota_period_start_at = fields.DateTime(allow_none=True)


def _get_quota_period(resource_type, user_id=None, email=None):
    """Periodic quota metadata; returns ``(dict|None, error|None, status)``."""
    users = _get_users(user_id, email) or None
    user = users[0] if users else None
    if not user:
        return None, "User not found.", 404
    if resource_type not in ResourceType._member_names_:
        return (
            None,
            f"Resource type '{resource_type}' is not one of the valid types: "
            f"{', '.join(ResourceType._member_names_)}",
            400,
        )
    resource = get_default_quota_resource(resource_type)
    user_resource = (
        Session.query(UserResource)
        .filter_by(user_id=user.id_, resource_id=resource.id_)
        .one_or_none()
    )
    if not user_resource:
        return None, "User resource not found.", 404
    return (
        {
            "quota_period_months": user_resource.quota_period_months,
            "quota_period_start_at": serialize_utc_datetime(
                user_resource.quota_period_start_at
            ),
        },
        None,
        200,
    )


def _get_quota(resource_type, user_id=None, email=None):
    """Quota limit + usage; returns ``(limit|None, usage|None, error|None, status)``."""
    users = _get_users(user_id, email) or None
    user = users[0] if users else None
    if not user:
        return None, None, "User not found.", 404
    if resource_type not in ResourceType._member_names_:
        return (
            None,
            None,
            f"Resource type '{resource_type}' is not one of the valid types: "
            f"{', '.join(ResourceType._member_names_)}",
            400,
        )
    quota_usage = user.get_quota_usage()
    limit = quota_usage.get(resource_type, {}).get("limit", {}).get("raw")
    usage = quota_usage.get(resource_type, {}).get("usage", {}).get("raw")
    return limit, usage, None, 200


def _check_secret(secret: Optional[str]):
    if not REANA_QUOTA_MANAGEMENT_SECRET:
        return JSONResponse(
            {"message": "Quota management endpoint is not configured."}, 403
        )
    if not _secrets.compare_digest(secret or "", REANA_QUOTA_MANAGEMENT_SECRET):
        return JSONResponse({"message": "Unauthorized"}, 401)
    return None


def _exactly_one_user(user_id, email):
    return int(bool(user_id)) + int(bool(email)) == 1


@router.get("/quota", summary="Get quota usage (management secret)")
def get_quota_usage(
    resource_type: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    x_quota_management_secret: Optional[str] = Header(None),
):
    """Return a user's quota limit/usage/period for a resource type."""
    denied = _check_secret(x_quota_management_secret)
    if denied:
        return denied
    if not user_id and not email:
        return JSONResponse({"message": "No user specified"}, 400)
    if user_id and email:
        return JSONResponse(
            {"message": "Exactly one of `user_id` or `email` must be provided."},
            400,
        )
    if not resource_type:
        return JSONResponse({"message": "Resource type is required."}, 400)
    if resource_type not in ResourceType._member_names_:
        return JSONResponse(
            {
                "message": f"Resource type '{resource_type}' does not exist. "
                f"Available resource types are: "
                f"{', '.join(ResourceType._member_names_)}"
            },
            400,
        )
    limit, usage, error_msg, status = _get_quota(resource_type, user_id, email)
    if error_msg:
        return JSONResponse({"message": error_msg}, status)
    period, period_error, period_status = _get_quota_period(
        resource_type, user_id, email
    )
    if period_error:
        return JSONResponse({"message": period_error}, period_status)
    if usage is None:
        return JSONResponse({"message": "Resource usage is not available."}, 500)
    if limit is None:
        return {
            "limit": -1,
            "usage": usage,
            "message": "Resource limit is not set.",
            **period,
        }
    return {"limit": limit, "usage": usage, "message": "OK", **period}


@router.post("/quota", summary="Set quota limit (management secret)")
def set_quota_limit(
    payload: dict = Body(...),
    x_quota_management_secret: Optional[str] = Header(None),
):
    """Set a user's quota limit for a resource type."""
    denied = _check_secret(x_quota_management_secret)
    if denied:
        return denied
    errors = SetQuotaLimitBodySchema().validate(payload)
    if errors:
        return JSONResponse({"message": f"Invalid request. Errors: {errors}"}, 400)
    limit = payload.get("limit")
    resource_type = payload.get("resource_type")
    user_id = payload.get("user_id")
    email = payload.get("email")
    if not _exactly_one_user(user_id, email):
        return JSONResponse(
            {"message": "Exactly one of `user_id` or `email` must be provided."},
            400,
        )
    msg, status_code, _ = _set_quota_limit(
        limit,
        resource_type=resource_type,
        user_ids=[user_id] if user_id else None,
        emails=[email] if email else None,
    )
    if status_code != 200:
        return JSONResponse({"message": msg}, status_code)
    _, usage, error_msg, status_code = _get_quota(resource_type, user_id, email)
    if status_code != 200:
        return JSONResponse({"message": error_msg}, status_code)
    period, error_msg, status_code = _get_quota_period(
        resource_type, user_id, email
    )
    if status_code != 200:
        return JSONResponse({"message": error_msg}, status_code)
    return {"limit": limit, "usage": usage, "message": "OK", **period}


@router.patch("/quota", summary="Patch quota period (management secret)")
def patch_quota(
    payload: dict = Body(...),
    x_quota_management_secret: Optional[str] = Header(None),
):
    """Update a user's quota period (months and/or start date)."""
    denied = _check_secret(x_quota_management_secret)
    if denied:
        return denied
    if (
        "quota_period_months" in payload
        and payload["quota_period_months"] is not None
        and type(payload["quota_period_months"]) is not int
    ):
        return JSONResponse(
            {
                "message": "Invalid request. Errors: {'quota_period_months': "
                "['Not a valid integer.']}"
            },
            400,
        )
    try:
        loaded = PatchQuotaBodySchema().load(payload)
    except ValidationError as error:
        return JSONResponse(
            {"message": f"Invalid request. Errors: {error.messages}"}, 400
        )
    user_id = loaded.get("user_id")
    email = loaded.get("email")
    resource_type = loaded.get("resource_type")
    if not _exactly_one_user(user_id, email):
        return JSONResponse(
            {"message": "Exactly one of `user_id` or `email` must be provided."},
            400,
        )
    period_kwargs = {}
    if "quota_period_months" in loaded:
        period_kwargs["quota_period_months"] = loaded.get("quota_period_months")
    if "quota_period_start_at" in loaded:
        period_kwargs["quota_period_start_at"] = loaded.get(
            "quota_period_start_at"
        )
    if not period_kwargs:
        return JSONResponse(
            {
                "message": "At least one of `quota_period_months` or "
                "`quota_period_start_at` must be provided."
            },
            400,
        )
    msg, status_code, _ = _set_quota_period(
        resource_type=resource_type, user_id=user_id, email=email, **period_kwargs
    )
    if status_code != 200:
        return JSONResponse({"message": msg}, status_code)
    limit, usage, error_msg, status_code = _get_quota(
        resource_type, user_id, email
    )
    if status_code != 200:
        return JSONResponse({"message": error_msg}, status_code)
    period, error_msg, status_code = _get_quota_period(
        resource_type, user_id, email
    )
    if status_code != 200:
        return JSONResponse({"message": error_msg}, status_code)
    if usage is None:
        return JSONResponse({"message": "Resource usage is not available."}, 500)
    if limit is None:
        return {
            "limit": -1,
            "usage": usage,
            "message": "Resource limit is not set.",
            **period,
        }
    return {"limit": limit, "usage": usage, "message": "OK", **period}
