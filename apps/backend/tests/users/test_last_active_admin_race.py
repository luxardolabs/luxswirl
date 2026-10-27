"""Two admins removing each other at the same moment still leave one active admin.

Each request alone sees another active admin and passes the check. The check
locks the active admin rows, so the second request waits for the first to
commit, then reads that committed change and is refused.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from app.core.exceptions import LastActiveAdminException
from app.models.user_model import User
from app.schemas.user_schema import UserUpdate
from app.services.core.user_core_service import UserCoreService
from fixtures.factories import make_user
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

svc = UserCoreService()


async def _seed(maker, *users: User) -> None:
    async with maker() as s:
        s.add_all(users)
        await s.commit()


async def _active_admins(maker) -> int:
    async with maker() as s:
        return await s.scalar(
            select(func.count(User.id)).where(
                User.role == "admin", User.is_active.is_(True)
            )
        )


async def _race(
    maker,
    first: Callable[[AsyncSession], Awaitable[object]],
    second: Callable[[AsyncSession], Awaitable[object]],
) -> None:
    """Run `first`, start `second` before `first` commits, then commit both."""
    async with maker() as s1, maker() as s2:
        await first(s1)
        pending = asyncio.create_task(second(s2))
        await asyncio.sleep(0.3)
        assert not pending.done(), "the second request did not wait for the first"
        await s1.commit()
        with pytest.raises(LastActiveAdminException, match="last active admin"):
            await pending
        await s2.rollback()


async def test_concurrent_demote_and_deactivate_leave_an_active_admin(
    worker_db,
) -> None:
    a, b = make_user(role="admin"), make_user(role="admin")
    await _seed(worker_db, a, b)

    await _race(
        worker_db,
        lambda s: svc.update_user(s, a.id, UserUpdate(is_active=False)),
        lambda s: svc.update_user(s, b.id, UserUpdate(role="editor")),
    )

    assert await _active_admins(worker_db) == 1


async def test_concurrent_delete_and_deactivate_leave_an_active_admin(
    worker_db,
) -> None:
    a, b = make_user(role="admin"), make_user(role="admin")
    await _seed(worker_db, a, b)

    await _race(
        worker_db,
        lambda s: svc.delete_user(s, a.id),
        lambda s: svc.update_user(s, b.id, UserUpdate(is_active=False)),
    )

    assert await _active_admins(worker_db) == 1


async def test_removing_one_of_two_admins_one_after_the_other_refuses_the_second(
    worker_db,
) -> None:
    a, b = make_user(role="admin"), make_user(role="admin")
    await _seed(worker_db, a, b)
    async with worker_db() as s1:
        await svc.update_user(s1, a.id, UserUpdate(is_active=False))
        await s1.commit()

    async with worker_db() as s2:
        with pytest.raises(LastActiveAdminException, match="last active admin"):
            await svc.update_user(s2, b.id, UserUpdate(role="editor"))

    assert await _active_admins(worker_db) == 1
