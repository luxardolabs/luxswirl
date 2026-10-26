"""A registration key is revoked once, and that revocation stands as recorded.

Through committed sessions (worker_db): a later revoke, a concurrent one, or
the web UI's soft delete of the revoked key changes neither when it was
revoked, by whom, nor why.
"""

from __future__ import annotations

import asyncio

import pytest
from app.core.exceptions import ValidationException
from app.crud.registration_key_crud import RegistrationKeyCRUD
from app.models.registration_key_model import RegistrationKey
from app.schemas.registration_key_schema import (
    RegistrationKeyCreate,
    RegistrationKeyRevoke,
)
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from sqlalchemy import select

pytestmark = pytest.mark.integration

svc = RegistrationKeyCoreService


async def _create(maker, name: str):
    async with maker() as s:
        key, _ = await svc.create_key(s, RegistrationKeyCreate(name=name))
        await s.commit()
        return key.id


async def _row(maker, key_id) -> RegistrationKey:
    async with maker() as s:
        row = await s.scalar(
            select(RegistrationKey).where(RegistrationKey.id == key_id)
        )
    assert row is not None
    return row


async def test_a_soft_delete_of_a_revoked_key_keeps_its_revocation(worker_db) -> None:
    key_id = await _create(worker_db, "del")
    async with worker_db() as s:
        first = await svc.revoke_key(
            s, key_id, RegistrationKeyRevoke(reason="leaked"), revoked_by="sec"
        )
        await s.commit()

    async with worker_db() as s:
        await svc.delete_key(s, key_id, hard_delete=False)
        await s.commit()

    row = await _row(worker_db, key_id)
    assert (row.revoked_reason, row.revoked_at, row.revoked_by) == (
        "leaked",
        first.revoked_at,
        "sec",
    )


async def test_a_soft_delete_of_an_active_key_revokes_it(worker_db) -> None:
    key_id = await _create(worker_db, "del-active")

    async with worker_db() as s:
        await svc.delete_key(s, key_id, hard_delete=False)
        await s.commit()

    row = await _row(worker_db, key_id)
    assert row.revoked_at is not None
    assert row.revoked_reason == "Deleted by admin"


async def test_two_revokes_at_once_one_wins(worker_db, monkeypatch) -> None:
    """Both read the key before either writes: only one may revoke it."""
    key_id = await _create(worker_db, "dbl")
    original = RegistrationKeyCRUD.get_by_id
    arrived = 0
    both = asyncio.Event()

    async def read_then_wait(db, kid):
        nonlocal arrived
        row = await original(db, kid)
        arrived += 1
        if arrived >= 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        return row

    monkeypatch.setattr(RegistrationKeyCRUD, "get_by_id", staticmethod(read_then_wait))

    async def revoke(who: str) -> str:
        async with worker_db() as s:
            try:
                await svc.revoke_key(
                    s, key_id, RegistrationKeyRevoke(reason=who), revoked_by=who
                )
                await s.commit()
                return "ok"
            except ValidationException:
                await s.rollback()
                return "refused"

    results = await asyncio.gather(revoke("first"), revoke("second"))
    monkeypatch.setattr(RegistrationKeyCRUD, "get_by_id", staticmethod(original))

    assert sorted(results) == ["ok", "refused"]
    row = await _row(worker_db, key_id)
    assert row.revoked_reason == row.revoked_by
