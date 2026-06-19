# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2026 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""GitLab integration endpoints (OAuth connect + projects + webhooks)."""

import logging
import secrets
import traceback
from typing import Optional

import requests
from fastapi import APIRouter, Body, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse, RedirectResponse, Response
from reana_commons.k8s.secrets import UserSecretsStore
from reana_db.database import Session
from reana_db.models import User

from reana_server.auth.deps import get_current_user
from reana_server.config import (
    REANA_GITLAB_OAUTH_APP_ID,
    REANA_GITLAB_OAUTH_APP_SECRET,
    REANA_GITLAB_URL,
    REANA_URL,
)
from reana_server.rest._oauth_state import (
    STATE_COOKIE,
    clear_state_cookie,
    consume_state,
    issue_state,
    safe_next_url,
)
from reana_server.gitlab_client import (
    GitLabClient,
    GitLabClientInvalidToken,
    GitLabClientRequestError,
)
from reana_server.utils import _format_gitlab_secrets, _get_gitlab_hook_id

router = APIRouter(tags=["gitlab"])

_RoleUser = Security(get_current_user, scopes=["reana:user"])
_CALLBACK_URL = f"{REANA_URL}/api/gitlab"
_WEBHOOK_URL = f"{REANA_URL}/api/workflows"


@router.get("/gitlab/connect", summary="Initiate GitLab connection")
def gitlab_connect(next: str = Query("/"), user: User = _RoleUser):
    """Redirect to GitLab's OAuth authorization endpoint."""
    response = RedirectResponse("placeholder", status_code=302)
    state = issue_state(response, next=safe_next_url(next))
    prepared = requests.PreparedRequest()
    prepared.prepare_url(
        REANA_GITLAB_URL + "/oauth/authorize",
        {
            "client_id": REANA_GITLAB_OAUTH_APP_ID,
            "redirect_uri": _CALLBACK_URL,
            "response_type": "code",
            "scope": "api",
            "state": state,
        },
    )
    response.headers["location"] = prepared.url
    return response


@router.get("/gitlab", summary="GitLab OAuth callback")
def gitlab_oauth(
    request: Request,
    code: Optional[str] = Query(None),
    state: str = Query(""),
    user: User = _RoleUser,
):
    """Exchange the code for a token and store it in the user's secrets."""
    try:
        if not code:
            return {"message": "OK"}
        data = consume_state(request.cookies.get(STATE_COOKIE), state)
        next_url = safe_next_url(data.get("next"))
        token_response = GitLabClient().oauth_token(
            {
                "client_id": REANA_GITLAB_OAUTH_APP_ID,
                "client_secret": REANA_GITLAB_OAUTH_APP_SECRET,
                "redirect_uri": _CALLBACK_URL,
                "code": code,
                "grant_type": "authorization_code",
            }
        ).json()
        access_token = token_response["access_token"]
        gitlab_user = GitLabClient(access_token=access_token).get_user().json()
        user_secrets = UserSecretsStore.fetch(user.id_)
        user_secrets.add_secrets(
            _format_gitlab_secrets(gitlab_user, access_token), overwrite=True
        )
        UserSecretsStore.update(user_secrets)
        response = RedirectResponse(next_url, status_code=302)
        clear_state_cookie(response)
        return response
    except HTTPException:
        raise  # invalid state -> 403
    except ValueError:
        return JSONResponse({"message": "Token is not valid."}, 403)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)


@router.get("/gitlab/projects", summary="List GitLab projects")
def gitlab_projects(
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: Optional[int] = Query(None, ge=1),
    user: User = _RoleUser,
):
    """List the user's GitLab projects (Maintainer+) with REANA hook status."""
    try:
        client = GitLabClient.from_k8s_secret(user.id_)
        result = client.get_projects(
            page=page,
            per_page=size,
            min_access_level=40,
            search=search,
            search_namespaces="true",
            simple="true",
        )
        projects = [
            {
                "id": project["id"],
                "name": project["name"],
                "path": project["path_with_namespace"],
                "url": project["web_url"],
                "hook_id": _get_gitlab_hook_id(project["id"], client),
            }
            for project in result.json()
        ]
        total = result.headers.get("x-total")
        return {
            "has_next": bool(result.headers.get("x-next-page")),
            "has_prev": bool(result.headers.get("x-prev-page")),
            "items": projects,
            "page": int(result.headers.get("x-page")),
            "size": int(result.headers.get("x-per-page")),
            "total": int(total) if total else None,
        }
    except GitLabClientInvalidToken as error:
        return JSONResponse({"message": str(error)}, 401)
    except GitLabClientRequestError as error:
        logging.error(str(error))
        return JSONResponse(
            {"message": "Project list could not be retrieved"},
            error.response.status_code,
        )
    except ValueError:
        return JSONResponse({"message": "Token is not valid."}, 403)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)


@router.post("/gitlab/webhook", status_code=201, summary="Create GitLab webhook")
def create_gitlab_webhook(payload: dict = Body(...), user: User = _RoleUser):
    """Create a REANA push/MR webhook on a GitLab project."""
    try:
        client = GitLabClient.from_k8s_secret(user.id_)
        if not user.gitlab_webhook_secret:
            # Per-user webhook secret authenticates GitLab deliveries
            # (AUTH_ARCHITECTURE.md §5.6).
            user.gitlab_webhook_secret = secrets.token_urlsafe(32)
            Session.commit()
        webhook_config = {
            "url": _WEBHOOK_URL,
            "push_events": True,
            "push_events_branch_filter": "master",
            "merge_requests_events": True,
            "enable_ssl_verification": False,
            "token": user.gitlab_webhook_secret,
        }
        webhook = client.create_webhook(
            payload["project_id"], webhook_config
        ).json()
        return JSONResponse({"id": webhook["id"]}, 201)
    except GitLabClientInvalidToken as error:
        return JSONResponse({"message": str(error)}, 401)
    except GitLabClientRequestError as error:
        logging.error(str(error))
        return JSONResponse(
            {"message": "Error while creating or deleting webhook"},
            error.response.status_code,
        )
    except ValueError:
        return JSONResponse({"message": "Token is not valid."}, 403)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)


@router.delete("/gitlab/webhook", summary="Delete GitLab webhook")
def delete_gitlab_webhook(payload: dict = Body(...), user: User = _RoleUser):
    """Delete an existing REANA webhook from a GitLab project."""
    try:
        client = GitLabClient.from_k8s_secret(user.id_)
        result = client.delete_webhook(payload["project_id"], payload["hook_id"])
        return Response(content=result.content, status_code=result.status_code)
    except GitLabClientInvalidToken as error:
        return JSONResponse({"message": str(error)}, 401)
    except GitLabClientRequestError as error:
        logging.error(str(error))
        return JSONResponse(
            {"message": "Error while creating or deleting webhook"},
            error.response.status_code,
        )
    except ValueError:
        return JSONResponse({"message": "Token is not valid."}, 403)
    except Exception as error:  # noqa: BLE001
        logging.error(traceback.format_exc())
        return JSONResponse({"message": str(error)}, 500)
