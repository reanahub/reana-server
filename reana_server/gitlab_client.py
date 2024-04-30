# This file is part of REANA.
# Copyright (C) 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server GitLab client."""

from typing import Dict, Optional, Union
from urllib.parse import quote_plus
import requests
import yaml

from reana_commons.k8s.secrets import UserSecretsStore

from reana_server.config import REANA_GITLAB_HOST


class GitLabClientException(Exception):
    """Base class for GitLab exceptions."""

    def __init__(self, message):
        """Initialise the GitLabClientException exception."""
        self.message = message

    def __str__(self):
        """Return the exception message."""
        return self.message


class GitLabClientRequestError(GitLabClientException):
    """Raised when a GitLab API request fails."""

    def __init__(self, response, message=None):
        """Initialise the GitLabClientRequestError exception."""
        message = message or f"GitLab API request failed: {response.status_code}"
        super().__init__(message)
        self.response = response


class GitLabClientInvalidToken(GitLabClientException):
    """Raised when GitLab token is invalid or missing."""

    def __init__(self, message=None):
        """Initialise the GitLabClientInvalidToken exception."""
        message = message or (
            "GitLab token invalid or missing, "
            "please go to your profile page on REANA "
            "and reconnect to GitLab."
        )
        super().__init__(message)


class GitLabClient:
    """Client for interacting with the GitLab API."""

    MAX_PER_PAGE = 100
    """Maximum number of items per page in paginated responses."""

    @classmethod
    def from_k8s_secret(cls, user_id, **kwargs):
        """
        Create a client instance taking the GitLab token from the user's k8s secret.

        :param user_id: User UUID.
        """
        user_secrets = UserSecretsStore.fetch(user_id)
        gitlab_token_secret = user_secrets.get_secret("gitlab_access_token")
        if not gitlab_token_secret:
            raise GitLabClientInvalidToken
        return cls(access_token=gitlab_token_secret.value_str, **kwargs)

    def __init__(
        self,
        host: str = REANA_GITLAB_HOST,
        access_token: Optional[str] = None,
        http_request=None,
    ):
        """Initialise the GitLab client.

        :param host: GitLab host (default: REANA_GITLAB_HOST)
        :param access_token: GitLab access token (default: unauthenticated)
        :param http_request: Function to make HTTP requests (default: requests.request).
        """
        self.access_token = access_token
        self.host = host
        self._http_request = (
            http_request if http_request is not None else requests.request
        )

    def _make_url(self, path: str, **kwargs: Dict[str, str]):
        quoted = {k: quote_plus(v) for k, v in kwargs.items()}
        return f"https://{self.host}/api/v4/{path.lstrip('/').format(**quoted)}"

    def _request(self, verb: str, url: str, params=None, data=None):
        res = self._http_request(verb, url, params=params, data=data)
        if res.status_code == 401:
            raise GitLabClientInvalidToken
        elif res.status_code >= 400:
            message = f"GitLab API request failed: {res.status_code}, {res.content}"
            try:
                response = res.json()
                if "message" in response:
                    message = f"GitLab API request failed: {res.status_code}, {response['message']}"
                elif "error_description" in response:
                    message = f"GitLab API request failed: {res.status_code}, {response['error_description']}"
            except Exception:
                pass
            raise GitLabClientRequestError(res, message)
        return res

    def _get(self, url, params=None):
        return self._request("GET", url, params)

    def _post(self, url, params=None, data=None):
        return self._request("POST", url, params, data)

    def _unroll_pagination(self, url, params):
        # use maximum allowed value to avoid too many network requests
        params["per_page"] = self.MAX_PER_PAGE
        res = self._get(url, params)
        while res:
            yield from res.json()
            next_url = res.links.get("next", {}).get("url")
            res = self._get(next_url) if next_url else None

    def oauth_token(self, data):
        """Request an OAuth token from GitLab.

        :param data: Dictionary with the following keys:
            - client_id: The client ID of the application.
            - client_secret: The client secret of the application.
            - code: The authorization code.
            - redirect_uri: The redirect URI of the application.
            - grant_type: The grant type of the request.
        """
        # _make_url is not used here as the URL does not contain `api/v4`
        url = f"https://{self.host}/oauth/token"
        return self._post(url, data=data)

    def get_file(
        self, project: Union[int, str], file_path: str, ref: Optional[str] = None
    ):
        """Get the content of a file in a GitLab repository.

        :param project: Project ID or name.
        :param file_path: Path to the file.
        :param ref: The name of a repository branch, tag or commit.
        """
        url = self._make_url(
            "projects/{project}/repository/files/{file_path}/raw",
            project=str(project),
            file_path=file_path,
        )
        params = {
            "access_token": self.access_token,
            "ref": ref,
        }
        return self._get(url, params)

    def get_projects(self, page: int = 1, per_page: Optional[int] = None, **kwargs):
        """Get a list of projects the user has access to.

        :param page: Page number.
        :param per_page: Number of projects per page.
        :param kwargs: Additional query parameters to customise and filter the results.
        """
        url = self._make_url("projects")
        params = {
            "access_token": self.access_token,
            "page": page,
            "per_page": per_page,
            **kwargs,
        }
        return self._get(url, params)

    def get_webhooks(
        self, project: Union[int, str], page: int = 1, per_page: Optional[int] = None
    ):
        """Get a list of webhooks for a project.

        :param project: Project ID or name.
        :param page: Page number.
        :param per_page: Number of webhooks per page.
        """
        url = self._make_url("projects/{project}/hooks", project=str(project))
        params = {
            "access_token": self.access_token,
            "page": page,
            "per_page": per_page,
        }
        return self._get(url, params)

    def get_all_webhooks(self, project: Union[int, str]):
        """Get all webhooks for a project.

        Compared to `get_webhooks`, this method returns a generator that yields
        all webhooks in the project, making multiple requests if necessary.

        :param project: Project ID or name.
        """
        url = self._make_url("projects/{project}/hooks", project=str(project))
        params = {"access_token": self.access_token}
        yield from self._unroll_pagination(url, params)

    def create_webhook(self, project: Union[int, str], config: Dict):
        """Create a webhook for a project.

        :param project: Project ID or name.
        :param config: Dictionary withe the webhook configuration.
            See https://docs.gitlab.com/ee/api/projects.html#add-project-hook
        """
        url = self._make_url("projects/{project}/hooks", project=str(project))
        params = {"access_token": self.access_token}
        return self._post(url, params, data=config)

    def delete_webhook(self, project: Union[int, str], hook_id: int):
        """Delete a webhook from a project.

        :param project: Project ID or name.
        :param hook_id: Webhook ID.
        """
        url = self._make_url(
            "projects/{project}/hooks/{hook_id}",
            project=str(project),
            hook_id=str(hook_id),
        )
        params = {
            "access_token": self.access_token,
        }
        return self._request("DELETE", url, params)

    def set_commit_build_status(
        self,
        project: Union[int, str],
        commit_sha: str,
        state: str,
        description: Optional[str] = None,
        name: str = "reana",
    ):
        """Set the status of a commit in a GitLab repository.

        :param project: Project ID or name.
        :param commit_sha: The commit SHA.
        :param state: The state of the status.
            Can be one of 'pending', 'running', 'success', 'failed', 'canceled'.
        :param description: A short description of the status.
        :param name: The name of the context (default: 'reana').
        """
        url = self._make_url(
            "projects/{project}/statuses/{commit_sha}",
            project=str(project),
            commit_sha=commit_sha,
        )
        params = {
            "access_token": self.access_token,
            "state": state,
            "description": description,
            "name": name,
        }
        return self._post(url, params)

    def get_user(self):
        """Get the user's profile."""
        url = self._make_url("user")
        params = {"access_token": self.access_token}
        return self._get(url, params)
