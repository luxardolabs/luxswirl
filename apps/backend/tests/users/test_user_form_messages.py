"""The user forms refuse bad input with the field and the reason.

The role, username and password a form sends are read by the user schema, so a
refusal names the field and never repeats the submitted value back.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.services.views.web_users_view_service import WebUsersViewService
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_PAYLOAD = "<img src=x onerror=alert(1)>"


async def test_creating_a_user_with_an_unknown_role_names_the_field(
    db: AsyncSession,
) -> None:
    user, error = await WebUsersViewService().create_user(
        db,
        username="new-operator",
        password="Str0ng!Passw0rd#",
        role=_PAYLOAD,
        full_name=None,
        is_active=True,
        must_change_password=False,
        created_by="admin",
    )

    assert user is None
    assert error is not None
    assert error.startswith("role: Input should be")
    assert _PAYLOAD not in error


async def test_creating_a_user_reports_every_refused_field(db: AsyncSession) -> None:
    _, error = await WebUsersViewService().create_user(
        db,
        username="",
        password="",
        role="viewer",
        full_name=None,
        is_active=True,
        must_change_password=False,
        created_by="admin",
    )

    assert error is not None
    assert "username: " in error
    assert "password: " in error


async def test_updating_a_user_with_an_unknown_role_is_a_schema_refusal(
    db: AsyncSession,
) -> None:
    """The app's 400 handler renders it as field and reason."""
    with pytest.raises(ValidationError) as refused:
        await WebUsersViewService().update_user(
            db,
            uuid4(),
            role=_PAYLOAD,
            full_name=None,
            is_active=True,
            must_change_password=False,
        )

    assert [e["loc"] for e in refused.value.errors()] == [("role",)]
