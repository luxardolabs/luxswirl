"""An unlock or recovery reset racing the sign-in that locks the account unlocks it.

`UserCoreService.unlock_user` and the recovery CLI's reset read the user with a
plain SELECT while the locking sign-in held the row uncommitted, so they read
`locked_until` as None. Their flush wrote only the columns that changed from
that stale read: the count went to 0, `locked_until` was never written, and the
account stayed locked although the unlock or reset said it worked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest
from app.models.user_model import User
from app.services.core.admin_bootstrap_core_service import AdminBootstrapCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.user_core_service import UserCoreService
from fixtures.factories import make_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

Reset = Callable[[AsyncSession, UUID, str], Awaitable[object]]


async def _race(worker_db, username: str, reset: Reset) -> User:
    """The 5th wrong guess (limit 5) locks the account, uncommitted, while the
    reset runs; the sign-in commits, then the reset does."""
    async with worker_db() as s:
        s.add(make_user(username=username, failed_login_attempts=4))
        await s.commit()
    async with worker_db() as s:
        uid = (await s.scalars(select(User.id).where(User.username == username))).one()
    auth = AuthCoreService()
    async with worker_db() as s_login, worker_db() as s_reset:
        assert await auth.authenticate_user(s_login, username, "wrong") is None

        async def do_reset() -> None:
            await reset(s_reset, uid, username)
            await s_reset.commit()

        task = asyncio.create_task(do_reset())
        await asyncio.sleep(0.5)
        await s_login.commit()
        await task
    async with worker_db() as s:
        return (await s.scalars(select(User).where(User.id == uid))).one()


async def _unlock(db: AsyncSession, uid: UUID, _login: str) -> object:
    return await UserCoreService().unlock_user(db, uid)


async def _cli_reset(db: AsyncSession, _uid: UUID, login: str) -> object:
    return await AdminBootstrapCoreService().reset_password(db, login)


async def test_control_the_sign_in_alone_locks_the_account(worker_db) -> None:
    async def nothing(_db: AsyncSession, _uid: UUID, _login: str) -> object:
        return None

    stored = await _race(worker_db, "lock_ctl", nothing)

    assert stored.locked_until is not None


async def test_an_unlock_racing_the_lock_unlocks(worker_db) -> None:
    stored = await _race(worker_db, "unlock_race", _unlock)

    assert stored.locked_until is None, "the unlock reported success, the lock stood"
    assert stored.failed_login_attempts == 0


async def test_a_cli_reset_racing_the_lock_unlocks(worker_db) -> None:
    stored = await _race(worker_db, "cli_race", _cli_reset)

    assert stored.locked_until is None, "the reset reported success, the lock stood"
    assert stored.failed_login_attempts == 0
