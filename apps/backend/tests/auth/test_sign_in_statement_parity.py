"""A sign-in refused before any password check sends the same statements whether
or not the account exists.

An unknown username, a locked account and an inactive account are all refused
after one bcrypt check against a fake hash, and they send the same statements.
Stated limits: a wrong password on an active account writes its failure count
(one UPDATE and its re-read more), and a real account's row lock gives the
transaction an id an unknown name's does not, so those still differ in database
work. Both sit far below one bcrypt check.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

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


async def test_an_unknown_username_signs_in_as_no_one_whatever_its_re_read_finds(
    worker_db,
) -> None:
    """An unknown username's re-read is by the nil UUID; a row holding that id
    still signs no one in."""
    await _add(worker_db, id=UUID(int=0), username="parity_nil_holder")

    async with worker_db() as session:
        signed_in = await AuthCoreService().authenticate_user(
            session, "parity_never_registered", "TestPass123!"
        )
        await session.rollback()

    assert signed_in is None
