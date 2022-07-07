# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server decorators tests."""

from flask import jsonify
import json
from unittest.mock import Mock, patch

from reana_db.models import User, UserToken

from reana_server.decorators import signin_required


def test_signing_required_with_token(default_user: User):
    """Test `signin_required` when user does not have a valid token."""
    # Delete user tokens
    UserToken.query.filter(UserToken.user_id == default_user.id_).delete()

    mock_endpoint = Mock(return_value=(jsonify(message="Ok"), 200))
    mock_current_user = Mock()
    mock_current_user.is_authenticated = True
    mock_current_user.email = default_user.email
    with patch("reana_server.decorators.current_user", mock_current_user):
        decorated_endpoint = signin_required()(mock_endpoint)
        response, code = decorated_endpoint()
        error = json.loads(response.get_data(as_text=True))
        mock_endpoint.assert_not_called()
        assert code == 401
        assert error["message"] == "User has no active tokens"
