"""A registration key revoked while its verification is in flight is refused.

verify_key_and_update_usage reads the active keys, spends bcrypt time on each
(about 250ms at cost 12), then counts the use with RegistrationKeyCRUD.record_usage.
That UPDATE filtered on id only, so a revoke committed inside that window was not
seen: the revoked key authenticated and its use was counted. It now re-checks
revoked_at in the same statement. Found by an adversarial pass.
"""

from __future__ import annotations

import pytest
from app.crud.registration_key_crud import RegistrationKeyCRUD
from app.schemas.registration_key_schema import (
    RegistrationKeyCreate,
    RegistrationKeyRevoke,
)
from app.services.core.registration_key_core_service import RegistrationKeyCoreService

pytestmark = pytest.mark.integration

svc = RegistrationKeyCoreService


async def _create(maker, name: str):
    async with maker() as s:
        key, plaintext = await svc.create_key(s, RegistrationKeyCreate(name=name))
        await s.commit()
        return key.id, plaintext


async def test_an_unrevoked_key_authenticates(worker_db) -> None:
    key_id, plaintext = await _create(worker_db, "ctl")
    async with worker_db() as s:
        got = await svc.verify_key_and_update_usage(s, plaintext)
        await s.commit()
    assert got is not None and got.id == key_id


async def test_a_key_revoked_before_the_call_is_refused(worker_db) -> None:
    key_id, plaintext = await _create(worker_db, "ctl2")
    async with worker_db() as s:
        await svc.revoke_key(s, key_id, RegistrationKeyRevoke(reason="leak"))
        await s.commit()
    async with worker_db() as s:
        assert await svc.verify_key_and_update_usage(s, plaintext) is None


async def test_key_revoked_during_verification_is_refused(
    worker_db, monkeypatch
) -> None:
    key_id, plaintext = await _create(worker_db, "race")
    original = RegistrationKeyCRUD.list_active

    async def list_then_concurrent_revoke(db):
        rows = await original(db)
        # An admin revokes the key on another connection while bcrypt runs.
        async with worker_db() as other:
            await svc.revoke_key(other, key_id, RegistrationKeyRevoke(reason="leak"))
            await other.commit()
        return rows

    monkeypatch.setattr(
        RegistrationKeyCRUD, "list_active", staticmethod(list_then_concurrent_revoke)
    )
    async with worker_db() as s:
        got = await svc.verify_key_and_update_usage(s, plaintext)
        await s.commit()
    assert got is None, (
        f"revoked key authenticated; usage_count={got.usage_count}, "
        f"revoked_at={got.revoked_at}"
    )
