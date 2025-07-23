"""REANA-Server OAuth utilities."""

import requests
from typing import Dict

from reana_db.database import Session
from reana_db.models import User
from sqlalchemy.exc import SQLAlchemyError

from reana_server.config import REANA_OAUTH_USERINFO_URL

def fetch_user_info(token: str) -> Dict:
    """Fetch user information from IdP's UserInfo endpoint.

    Args:
        token: Access token to use for UserInfo request

    Returns:
        Dict: User info from IdP

    Raises:
        ValueError: If UserInfo request fails
    """
    if not REANA_OAUTH_USERINFO_URL:
        raise ValueError("UserInfo endpoint not configured")

    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(REANA_OAUTH_USERINFO_URL, headers=headers)
        if response.status_code != 200:
            raise ValueError(f"Failed to fetch user info: {response.status_code}")

        user_info = response.json()
        # Validate required fields
        if not user_info.get("email"):
            raise ValueError("Email not provided in UserInfo response")

        return user_info
    except requests.RequestException as e:
        raise ValueError(f"Error communicating with IdP: {str(e)}")


def create_or_update_user(idp_id: str, user_info: Dict) -> User:
    """Create or update user record with information from IdP.

    Args:
        idp_id: Subject identifier from IdP
        user_info: User information from IdP's UserInfo endpoint

    Returns:
        User: Created or updated user record

    Raises:
        ValueError: If required user info is missing or database error occurs
    """
    try:
        email = user_info["email"]
        if not email:
            raise ValueError("Email is required in UserInfo response from IdP")

        user = Session.query(User).filter_by(idp_id=idp_id).one_or_none()

        # If not found, try by email as fallback
        if not user:
            user = Session.query(User).filter_by(email=email).one_or_none()
            if user:
                # If found by email, update idp_id
                user.idp_id = idp_id

        if not user:
            # Create new user
            user_parameters = {
                "email": email,
                "idp_id": idp_id,
                "full_name": user_info.get("name", email),
                "username": user_info.get("preferred_username", email)
            }
            user = User(**user_parameters)
            Session.add(user)
            Session.commit()
            return user

        # Only update user info if it has changed
        if (user.email != email or
            user.full_name != user_info.get("name", email) or
            user.username != user_info.get("preferred_username", email)):

            user.email = email
            user.full_name = user_info.get("name", email)
            user.username = user_info.get("preferred_username", email)
            Session.commit()

        return user
    except SQLAlchemyError as e:
        Session.rollback()
        raise ValueError(f"Database error: {str(e)}")
    except Exception as e:
        raise ValueError(f"Error creating or updating user: {str(e)}")


def create_or_update_user_from_idp(token: str, user_idp_id: str) -> User:
    """Create or update user record by fetching info from IdP.

    Args:
        token: Access token to fetch user info
        user_idp_id: Subject identifier from IdP (e.g., sub claim)

    Returns:
        User: Created or updated user record

    Raises:
        ValueError: If IdP communication fails or user creation fails
    """
    try:
        user_info = fetch_user_info(token)
        return create_or_update_user(user_idp_id, user_info)
    except Exception as e:
        raise ValueError(f"Failed to create/update user: {str(e)}")
