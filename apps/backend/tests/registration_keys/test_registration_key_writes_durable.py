"""Every registration-key write survives its owner's commit (durability locks).

Locks for moving RegistrationKeyCoreService's session writes into crud
(`fw.no_session_mutation_outside_crud`). Each test owns the session, calls the
real service method, commits, and reads back on a SEPARATE connection.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from app.models.registration_key_model import RegistrationKey
from app.schemas.registration_key_schema import (
    RegistrationKeyCreate,
    RegistrationKeyRevoke,
    RegistrationKeyUpdate,
)
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from sqlalchemy import select

pytestmark = pytest.mark.integration

svc = RegistrationKeyCoreService


async def _create(maker) -> tuple[UUID, str]:
    async with maker() as s:
        key, plaintext = await svc.create_key(s, RegistrationKeyCreate(name="lab"))
        await s.commit()
        return key.id, plaintext


async def _stored(maker, key_id: UUID) -> RegistrationKey | None:
    async with maker() as other:
        return await other.scalar(
            select(RegistrationKey).where(RegistrationKey.id == key_id)
        )


async def test_create_key_is_committed(worker_db) -> None:
    key_id, _ = await _create(worker_db)
    stored = await _stored(worker_db, key_id)
    assert stored is not None and stored.name == "lab"


async def test_update_key_is_committed(worker_db) -> None:
    key_id, _ = await _create(worker_db)
    async with worker_db() as s:
        await svc.update_key(s, key_id, RegistrationKeyUpdate(name="lab-2"))
        await s.commit()
    stored = await _stored(worker_db, key_id)
    assert stored is not None and stored.name == "lab-2"


async def test_revoke_key_is_committed(worker_db) -> None:
    key_id, _ = await _create(worker_db)
    async with worker_db() as s:
        await svc.revoke_key(s, key_id, RegistrationKeyRevoke(reason="leaked"))
        await s.commit()
    stored = await _stored(worker_db, key_id)
    assert stored is not None and stored.revoked_at is not None
    assert stored.revoked_reason == "leaked"


@pytest.mark.parametrize("hard", [True, False])
async def test_delete_key_is_committed(worker_db, hard: bool) -> None:
    key_id, _ = await _create(worker_db)
    async with worker_db() as s:
        await svc.delete_key(s, key_id, hard_delete=hard)
        await s.commit()
    stored = await _stored(worker_db, key_id)
    if hard:
        assert stored is None
    else:
        assert stored is not None and stored.revoked_at is not None


async def test_each_verified_use_is_counted(worker_db) -> None:
    key_id, plaintext = await _create(worker_db)
    for _ in range(2):
        async with worker_db() as s:
            used = await svc.verify_key_and_update_usage(s, plaintext)
            assert used is not None and used.id == key_id
            await s.commit()
    stored = await _stored(worker_db, key_id)
    assert stored is not None and stored.usage_count == 2
    assert stored.last_used_at is not None
