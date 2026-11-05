"""Migration 011 against the keys a real stack may have: none, another one,
or only the server's file."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.config import settings
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "011_encrypt_maintenance_job_params.py"
)


def _run(connection: Any, step: str) -> None:
    spec = importlib.util.spec_from_file_location("rev011_keys", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


async def _stored(db: AsyncSession, job_id: Any) -> str:
    return await db.scalar(
        text("SELECT params::text FROM maintenance_jobs WHERE id = :id"),
        {"id": job_id},
    )


async def _encrypted_row(db: AsyncSession, params: dict) -> Any:
    """A job stored by the JSON column, then upgraded (encrypted)."""
    await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    job_id = uuid4()
    await db.execute(
        text(
            "INSERT INTO maintenance_jobs (id, kind, params, status, created_at)"
            " VALUES (:id, 'bulk_check_import', CAST(:p AS json), 'queued', now())"
        ),
        {"id": job_id, "p": json.dumps(params)},
    )
    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))
    return job_id


PARAMS = {"agent_id": str(uuid4()), "mode": "merge", "checks": [{"name": "x"}]}


async def test_control_downgrade_with_the_same_key_restores_params(
    db: AsyncSession, field_key: str, monkeypatch
) -> None:
    job_id = await _encrypted_row(db, PARAMS)
    await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    assert json.loads(await _stored(db, job_id)) == PARAMS
    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))


async def test_downgrade_without_the_key_does_not_wipe_params(
    db: AsyncSession, field_key: str, monkeypatch
) -> None:
    """Downgrading with no key configured must fail, not write `{}` over every
    encrypted row."""
    job_id = await _encrypted_row(db, PARAMS)
    before = await _stored(db, job_id)
    monkeypatch.setattr(settings.security, "field_encryption_key", "")
    with pytest.raises(RuntimeError, match="no encryption key"):
        await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    assert await _stored(db, job_id) == before


async def test_downgrade_under_another_key_does_not_wipe_params(
    db: AsyncSession, field_key: str, monkeypatch
) -> None:
    """A row the downgrade cannot decrypt must not silently become `{}`."""
    job_id = await _encrypted_row(db, PARAMS)
    before = await _stored(db, job_id)
    monkeypatch.setattr(
        settings.security, "field_encryption_key", Fernet.generate_key().decode()
    )
    with pytest.raises(RuntimeError, match="do not decrypt"):
        await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    assert await _stored(db, job_id) == before


async def test_upgrade_encrypts_with_the_configured_key(
    db: AsyncSession, monkeypatch
) -> None:
    """Upgrade encrypts with SECURITY__FIELD_ENCRYPTION_KEY, as the server reads it."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings.security, "field_encryption_key", key)
    job_id = await _encrypted_row(db, PARAMS)
    stored = await _stored(db, job_id)
    assert json.loads(Fernet(key.encode()).decrypt(stored.encode())) == PARAMS
