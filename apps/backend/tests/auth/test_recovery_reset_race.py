"""A session purge ends every session, including one a racing sign-in opens.

The recovery CLI's reset (AdminBootstrapCoreService.reset_password) ended the
user's sessions without first locking the user row, and the session factory has
autoflush off: its DELETE of sessions ran before its UPDATE waited on the
sign-in's row lock, so a sign-in with the old password already holding the row
committed a session the reset never saw. A role change (UserCoreService.update_user)
purged the same way. Every purge now locks the user row first.
"""

from __future__ import annotations

import asyncio

import pytest
from app.models.enum_model import UserRole
from app.models.session_model import Session
from app.models.user_model import User
from app.schemas.user_schema import UserUpdate
from app.services.core.admin_bootstrap_core_service import AdminBootstrapCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.user_core_service import UserCoreService
from fixtures.factories import make_user
from sqlalchemy import select

pytestmark = pytest.mark.integration

RIGHT = "TestPass123!"


async def _race(worker_db, username: str, reset) -> list[Session]:
    async with worker_db() as s:
        s.add(make_user(username=username))
        await s.commit()
    async with worker_db() as s:
        uid = (await s.scalars(select(User.id).where(User.username == username))).one()
    auth = AuthCoreService()
    async with worker_db() as s_login, worker_db() as s_reset:
        user = await auth.authenticate_user(s_login, username, RIGHT)
        assert user is not None  # old password accepted; row lock held

        async def do_reset() -> None:
            await reset(s_reset, uid, username)
            await s_reset.commit()

        task = asyncio.create_task(do_reset())
        await asyncio.sleep(0.5)
        await auth.create_session(s_login, user.id)
        await s_login.commit()
        await task
    async with worker_db() as s:
        return list(
            (await s.scalars(select(Session).where(Session.user_id == uid))).all()
        )


async def test_control_admin_reset_after_sign_in_ends_its_session(worker_db) -> None:
    async def reset(db, uid, _login):
        await AuthCoreService().reset_password(db, uid, "Br4nd-New!Pass-xyz")

    assert await _race(worker_db, "admin_reset", reset) == []


async def test_cli_reset_after_sign_in_ends_its_session(worker_db) -> None:
    async def reset(db, _uid, login):
        await AdminBootstrapCoreService().reset_password(db, login)

    sessions = await _race(worker_db, "cli_reset", reset)
    assert sessions == [], (
        "a session signed in with the old password outlived the CLI reset"
    )


async def test_a_role_change_ends_a_session_signed_in_in_flight(worker_db) -> None:
    """An editor demoted to viewer must sign in again. A sign-in in flight
    (its session inserted, uncommitted) when the change starts is purged too."""
    async with worker_db() as s:
        user = make_user(username="role_ed", role=UserRole.EDITOR)
        s.add(user)
        await s.commit()
        uid = user.id
    auth = AuthCoreService()
    async with worker_db() as s_login, worker_db() as s_change:
        signed_in = await auth.authenticate_user(s_login, "role_ed", RIGHT)
        assert signed_in is not None
        await auth.create_session(s_login, signed_in.id)
        change = asyncio.create_task(
            UserCoreService().update_user(
                s_change, uid, UserUpdate(role=UserRole.VIEWER)
            )
        )
        await asyncio.sleep(0.8)
        await s_login.commit()
        await change
        await s_change.commit()
    async with worker_db() as s:
        left = (await s.scalars(select(Session).where(Session.user_id == uid))).all()
    assert left == [], "a session opened at the old role outlived the role change"
