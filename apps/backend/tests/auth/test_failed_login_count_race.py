"""Concurrent wrong passwords for one account each count.

A wrong password raises the account's failed-login count, which locks the
account at the limit. When each request read the count and wrote back its read
value plus one, two requests that read the same value both wrote the same
count: one failure was lost, and an attacker guessing in parallel got more
guesses than the limit. The count is now raised in the statement that writes
it, so the second request waits for the first and adds to its count.
"""

from __future__ import annotations

import asyncio

import pytest
from app.models.user_model import User
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def test_two_concurrent_wrong_passwords_both_count(worker_db) -> None:
    async with worker_db() as s:
        s.add(make_user(username="guessed"))
        await s.commit()

    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        assert await auth.authenticate_user(s1, "guessed", "wrong-1") is None
        second = asyncio.create_task(auth.authenticate_user(s2, "guessed", "wrong-2"))
        await asyncio.sleep(0.5)
        await s1.commit()
        assert await second is None
        await s2.commit()

    async with worker_db() as s:
        count = await s.scalar(
            select(User.failed_login_attempts).where(User.username == "guessed")
        )
    assert count == 2


async def test_the_limit_reached_concurrently_locks_the_account(worker_db) -> None:
    async with worker_db() as s:
        s.add(make_user(username="limit", failed_login_attempts=3))
        await s.commit()

    auth = AuthCoreService()
    async with worker_db() as s1, worker_db() as s2:
        await auth.authenticate_user(s1, "limit", "wrong-1")
        second = asyncio.create_task(auth.authenticate_user(s2, "limit", "wrong-2"))
        await asyncio.sleep(0.5)
        await s1.commit()
        await second
        await s2.commit()

    async with worker_db() as s:
        user = (await s.scalars(select(User).where(User.username == "limit"))).one()
    assert user.failed_login_attempts == 5
    assert user.locked_until is not None
