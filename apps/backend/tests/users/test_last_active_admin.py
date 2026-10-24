"""The last active admin cannot be demoted, deactivated or deleted.

Someone must always be able to sign in and manage users; with no active admin
left, only the server's bootstrap CLI gets one back. An inactive admin does not
count: it cannot sign in.
"""

from __future__ import annotations

import pytest
from app.schemas.user_schema import UserUpdate
from app.services.core.user_core_service import UserCoreService
from fixtures.factories import make_user
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_CHANGES = [
    pytest.param({"role": "editor"}, id="demote"),
    pytest.param({"is_active": False}, id="deactivate"),
]


async def _only_admin(db: AsyncSession, *others: dict) -> object:
    admin = make_user(role="admin", is_active=True)
    db.add(admin)
    for other in others:
        db.add(make_user(**other))
    await db.flush()
    return admin


@pytest.mark.parametrize("change", _CHANGES)
async def test_the_last_active_admin_keeps_the_role(
    db: AsyncSession, change: dict
) -> None:
    admin = await _only_admin(db, {"role": "admin", "is_active": False})

    with pytest.raises(ValueError, match="last active admin"):
        await UserCoreService().update_user(
            db, admin.id, UserUpdate.model_validate(change)
        )

    await db.refresh(admin)
    assert (admin.role, admin.is_active) == ("admin", True)


@pytest.mark.parametrize("change", _CHANGES)
async def test_with_another_active_admin_the_change_is_made(
    db: AsyncSession, change: dict
) -> None:
    admin = await _only_admin(db, {"role": "admin", "is_active": True})

    updated = await UserCoreService().update_user(
        db, admin.id, UserUpdate.model_validate(change)
    )

    assert (updated.role, updated.is_active) != ("admin", True)


async def test_the_last_active_admin_can_still_edit_its_own_name(
    db: AsyncSession,
) -> None:
    admin = await _only_admin(db)

    updated = await UserCoreService().update_user(
        db, admin.id, UserUpdate.model_validate({"full_name": "Ops", "role": "admin"})
    )

    assert updated.full_name == "Ops"


async def test_the_last_active_admin_cannot_be_deleted_beside_an_inactive_one(
    db: AsyncSession,
) -> None:
    admin = await _only_admin(db, {"role": "admin", "is_active": False})

    with pytest.raises(ValueError, match="last active admin"):
        await UserCoreService().delete_user(db, admin.id)


async def test_an_inactive_admin_can_be_deleted(db: AsyncSession) -> None:
    await _only_admin(db)
    inactive = make_user(role="admin", is_active=False)
    db.add(inactive)
    await db.flush()

    assert await UserCoreService().delete_user(db, inactive.id) is True
