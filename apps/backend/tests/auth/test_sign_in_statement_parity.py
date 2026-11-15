"""A refused sign-in sends the same statements whether or not the account exists.

An unknown username, a locked account and an inactive account are all refused
after one bcrypt check against a fake hash. The database work before that check
is the same for all three as well, so neither the answer nor the number of round
trips tells a caller which usernames are real.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.datetime_utils import utc_now
from app.db.database import engine
from app.models.user_model import User
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from sqlalchemy import event

pytestmark = pytest.mark.integration


async def _add(worker_db, **fields) -> User:
    async with worker_db() as session:
        user = make_user(**fields)
        session.add(user)
        await session.commit()
        return user


async def _statements(worker_db, username: str) -> int:
    """How many statements one refused sign-in for `username` sends."""
    count = 0

    def on_execute(*_args, **_kwargs) -> None:
        nonlocal count
        count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", on_execute)
    try:
        async with worker_db() as session:
            refused = await AuthCoreService().authenticate_user(
                session, username, "wrong"
            )
            assert refused is None
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", on_execute)
    return count


async def test_unknown_locked_and_inactive_sign_ins_send_the_same_statements(
    worker_db,
) -> None:
    await _add(
        worker_db,
        username="parity_locked",
        failed_login_attempts=5,
        locked_until=utc_now() + timedelta(minutes=30),
    )
    await _add(worker_db, username="parity_inactive", is_active=False)

    unknown = await _statements(worker_db, "parity_nobody")
    unknown_again = await _statements(worker_db, "parity_nobody_either")
    locked = await _statements(worker_db, "parity_locked")
    inactive = await _statements(worker_db, "parity_inactive")

    assert unknown == unknown_again
    assert (locked, inactive) == (unknown, unknown)
