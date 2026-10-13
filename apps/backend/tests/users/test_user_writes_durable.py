"""Every user write survives its owner's commit (durability locks).

Locks for moving UserCoreService's session writes into crud
(`fw.no_session_mutation_outside_crud`). Each test owns the session, calls the
real service method, commits, and reads back on a SEPARATE connection.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.models.user_model import User
from app.schemas.user_schema import UserCreate, UserUpdate
from app.services.core.user_core_service import UserCoreService
from fixtures.factories import make_user
from sqlalchemy import select

pytestmark = pytest.mark.integration

svc = UserCoreService()


async def _stored(maker, user_id: UUID) -> User | None:
    async with maker() as other:
        return await other.scalar(select(User).where(User.id == user_id))


async def _seed(maker, *users: User) -> None:
    async with maker() as s:
        s.add_all(users)
        await s.commit()


async def test_create_user_is_committed(worker_db) -> None:
    async with worker_db() as s:
        created = await svc.create_user(
            s, UserCreate(username="ops", password="Sup3rSecret!pw", role="viewer")
        )
        await s.commit()
    stored = await _stored(worker_db, created.id)
    assert stored is not None and stored.username == "ops"


async def test_update_user_is_committed(worker_db) -> None:
    user = make_user(full_name="Before")
    await _seed(worker_db, user)
    async with worker_db() as s:
        await svc.update_user(s, user.id, UserUpdate(full_name="After"))
        await s.commit()
    stored = await _stored(worker_db, user.id)
    assert stored is not None and stored.full_name == "After"


async def test_delete_user_is_committed(worker_db) -> None:
    keep, gone = make_user(role="admin"), make_user(role="viewer")
    await _seed(worker_db, keep, gone)
    async with worker_db() as s:
        assert await svc.delete_user(s, gone.id) is True
        await s.commit()
    assert await _stored(worker_db, gone.id) is None
    assert await _stored(worker_db, keep.id) is not None


async def test_unlock_user_is_committed(worker_db) -> None:
    user = make_user(
        failed_login_attempts=5, locked_until=utc_now() + timedelta(hours=1)
    )
    await _seed(worker_db, user)
    async with worker_db() as s:
        await svc.unlock_user(s, user.id)
        await s.commit()
    stored = await _stored(worker_db, user.id)
    assert stored is not None and stored.locked_until is None
    assert stored.failed_login_attempts == 0
