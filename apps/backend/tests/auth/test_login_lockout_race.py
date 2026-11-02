"""Sign-ins for one account are judged one at a time.

A sign-in read the account's lock with a plain SELECT, checked the password
against the real hash, and only then counted the failure. The wrong guess that
reached the limit locked the account, but its lock stayed uncommitted until its
response was sent, so a guess already in flight still read the account as
unlocked, was checked against the real hash, and signed in when it was right.
A success in flight beside the locking failure also reset the count while the
lock stood.

A sign-in now reads the user row FOR UPDATE before bcrypt: a concurrent
sign-in for the same account waits, then reads the committed lock. One that
waits past the login's lock wait is refused with a typed 429, not a 500 from
the request's statement timeout.
"""

from __future__ import annotations

import asyncio

import pytest
from app.core.exceptions import LoginBusyException
from app.models.user_model import User
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from sqlalchemy import func, select

pytestmark = pytest.mark.integration

RIGHT = "TestPass123!"


async def _stored(worker_db, username: str) -> User:
    async with worker_db() as s:
        return (await s.scalars(select(User).where(User.username == username))).one()


async def _seed(worker_db, username: str, failed: int) -> None:
    async with worker_db() as s:
        s.add(make_user(username=username, failed_login_attempts=failed))
        await s.commit()


async def test_control_the_limit_committed_refuses_the_right_password(
    worker_db,
) -> None:
    await _seed(worker_db, "seq", 4)
    auth = AuthCoreService()
    async with worker_db() as s1:
        assert await auth.authenticate_user(s1, "seq", "wrong") is None
        await s1.commit()
    async with worker_db() as s2:
        assert await auth.authenticate_user(s2, "seq", RIGHT) is None


async def test_a_guess_in_flight_waits_for_the_lock_and_is_refused(worker_db) -> None:
    """The 5th wrong guess (limit 5) locks the account; a right guess sent
    while that lock is uncommitted waits for it and is refused."""
    await _seed(worker_db, "par", 4)
    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        assert await auth.authenticate_user(s1, "par", "wrong") is None
        sixth = asyncio.create_task(auth.authenticate_user(s2, "par", RIGHT))
        await asyncio.sleep(0.5)
        assert not sixth.done(), "the 6th guess was judged before the 5th committed"
        await s1.commit()
        assert await sixth is None, "6th guess beyond limit 5 signed in"
        await s2.commit()

    user = await _stored(worker_db, "par")
    assert user.failed_login_attempts == 5
    assert user.locked_until is not None, "the lock from the 5th failure was erased"


async def test_a_success_after_a_failure_in_flight_counts_from_it(worker_db) -> None:
    """Below the limit, a right password sent while a failure is uncommitted
    waits for it and then signs in, resetting the committed count."""
    await _seed(worker_db, "below", 2)
    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        assert await auth.authenticate_user(s1, "below", "wrong") is None
        ok = asyncio.create_task(auth.authenticate_user(s2, "below", RIGHT))
        await asyncio.sleep(0.5)
        assert not ok.done()
        await s1.commit()
        assert await ok is not None
        await s2.commit()

    user = await _stored(worker_db, "below")
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


async def test_a_sign_in_that_waits_too_long_is_refused_as_busy(
    worker_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sign-in queued behind another for the same account past the lock wait
    gets the typed 429, not the statement timeout's 500, and its session stays
    usable."""
    monkeypatch.setattr(AuthCoreService, "LOGIN_LOCK_WAIT_MS", 200)
    await _seed(worker_db, "busy", 0)
    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        assert await auth.authenticate_user(s1, "busy", "wrong") is None
        with pytest.raises(LoginBusyException) as refused:
            await auth.authenticate_user(s2, "busy", RIGHT)
        assert refused.value.status_code == 429
        # The refusal rolled back only its savepoint: the session still works.
        assert (
            await s2.scalar(select(User.username).where(User.username == "busy"))
        ) == "busy"
        await s1.commit()
        await s2.rollback()

    user = await _stored(worker_db, "busy")
    assert user.failed_login_attempts == 1


async def test_the_lock_wait_does_not_outlast_the_login(worker_db) -> None:
    """The login's lock wait is scoped to its row read: later statements in
    the same transaction run under the server's own lock_timeout."""
    await _seed(worker_db, "scoped", 0)
    auth = AuthCoreService()
    async with worker_db() as s:
        default = await s.scalar(select(func.current_setting("lock_timeout")))
        assert await auth.authenticate_user(s, "scoped", RIGHT) is not None
        assert await s.scalar(select(func.current_setting("lock_timeout"))) == default
        await s.rollback()
