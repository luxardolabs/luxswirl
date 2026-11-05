"""Revision c24bfe691550's check_config fix-up reaches the encrypted configs.

c24bfe691550 ran in alembic's own process, which had no key when the server's
key lives only in its file, so it skipped every encrypted config. Two halves:
alembic/env.py now loads the server's key before migrating, and revision
609ddde70f0b re-applies the fix-up to every config with that key, writing
only what it changes and only encrypted. The test suite's schema is already
at head, so the revision's `migrate` runs directly against rows shaped like
an older version's.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import logging
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from app.core.config import settings
from app.models.check_model import Check
from cryptography.fernet import Fernet
from fixtures._db_url import TEST_DATABASE_URL
from fixtures.factories import make_agent, make_check
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "016_closed_set_encrypted_configs.py"
)


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "closed_set_encrypted_configs", _REVISION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _migrate(db: AsyncSession) -> None:
    revision = _revision()
    await db.run_sync(lambda session: revision.migrate(session.connection()))
    db.expire_all()


async def _check(db: AsyncSession, agent_id: UUID, raw: str) -> UUID:
    """A check whose check_config column holds exactly ``raw``."""
    check = make_check(
        agent_id=agent_id,
        check_type="http",
        target="https://legacy.test",
        check_config={},
        enabled=True,
    )
    db.add(check)
    await db.flush()
    await db.execute(
        text("UPDATE checks SET check_config = :raw WHERE id = :id"),
        {"raw": raw, "id": check.id},
    )
    return check.id


async def _raw(db: AsyncSession, check_id: UUID) -> str:
    return await db.scalar(
        text("SELECT check_config FROM checks WHERE id = :id"), {"id": check_id}
    )


def _encrypted(key: str, config: dict[str, Any]) -> str:
    return Fernet(key.encode()).encrypt(json.dumps(config).encode()).decode()


def _decrypted(key: str, raw: str) -> dict[str, Any]:
    return json.loads(Fernet(key.encode()).decrypt(raw.encode()))


async def _agent(db: AsyncSession) -> UUID:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    return agent.id


async def test_encrypted_configs_are_brought_into_their_sets(
    db: AsyncSession, field_key: str
) -> None:
    agent_id = await _agent(db)
    lower = await _check(
        db, agent_id, _encrypted(field_key, {"http_method": "options", "x": 1})
    )
    bad_method = await _check(
        db, agent_id, _encrypted(field_key, {"http_method": "FETCH", "y": 2})
    )
    bad_record = await _check(
        db, agent_id, _encrypted(field_key, {"record_type": "ANY", "z": 3})
    )
    empty = await _check(db, agent_id, _encrypted(field_key, {"record_type": " "}))

    await _migrate(db)

    rows = {
        c.id: (c.check_config, c.enabled)
        for c in await db.scalars(
            select(Check).where(Check.id.in_([lower, bad_method, bad_record, empty]))
        )
    }
    assert rows[lower] == ({"http_method": "OPTIONS", "x": 1}, True)
    assert rows[bad_method] == ({"y": 2}, False)
    assert rows[bad_record] == ({"z": 3}, False)
    assert rows[empty] == ({}, True)
    for check_id in (lower, bad_method, bad_record, empty):
        raw = await _raw(db, check_id)
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)
        _decrypted(field_key, raw)


async def test_a_config_already_in_its_sets_is_not_rewritten(
    db: AsyncSession, field_key: str
) -> None:
    """Idempotent and minimal: an in-set encrypted config and a plaintext one
    are left byte for byte, and a second run changes nothing."""
    agent_id = await _agent(db)
    clean = await _check(db, agent_id, _encrypted(field_key, {"http_method": "GET"}))
    plain = await _check(db, agent_id, json.dumps({"record_type": "MX"}))
    fixed = await _check(db, agent_id, _encrypted(field_key, {"http_method": "put"}))
    before = {i: await _raw(db, i) for i in (clean, plain)}

    await _migrate(db)
    after_first = await _raw(db, fixed)
    await _migrate(db)

    assert {i: await _raw(db, i) for i in (clean, plain)} == before
    assert await _raw(db, fixed) == after_first
    assert _decrypted(field_key, after_first) == {"http_method": "PUT"}


async def test_a_plaintext_config_it_fixes_is_written_encrypted(
    db: AsyncSession, field_key: str
) -> None:
    agent_id = await _agent(db)
    plain = await _check(db, agent_id, json.dumps({"http_method": "post"}))

    await _migrate(db)

    assert _decrypted(field_key, await _raw(db, plain)) == {"http_method": "POST"}


async def test_without_a_key_encrypted_configs_fail_the_upgrade(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a silent skip: the configs c24bfe691550 missed would stay unfixed."""
    key = Fernet.generate_key().decode()
    agent_id = await _agent(db)
    check_id = await _check(db, agent_id, _encrypted(key, {"http_method": "get"}))
    before = await _raw(db, check_id)
    monkeypatch.setattr(settings.security, "field_encryption_key", "")

    with pytest.raises(RuntimeError, match="no encryption key"):
        await _migrate(db)

    assert await _raw(db, check_id) == before


async def test_without_a_key_and_no_encrypted_config_it_is_a_no_op(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = await _agent(db)
    check_id = await _check(db, agent_id, json.dumps({"http_method": "GET"}))
    monkeypatch.setattr(settings.security, "field_encryption_key", "")

    await _migrate(db)

    assert await _raw(db, check_id) == json.dumps({"http_method": "GET"})


async def test_a_config_under_another_key_is_skipped_and_named(
    db: AsyncSession, field_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    agent_id = await _agent(db)
    other = Fernet.generate_key().decode()
    foreign = await _check(db, agent_id, _encrypted(other, {"http_method": "get"}))
    before = await _raw(db, foreign)

    with caplog.at_level(logging.WARNING, logger="alembic.runtime.migration"):
        await _migrate(db)

    assert await _raw(db, foreign) == before
    assert str(foreign) in caplog.text


def _run_alembic_env() -> None:
    """Run alembic/env.py as `alembic` does, offline: env.py loads the key
    before it chooses a mode, and offline it opens no connection (each test's
    one connection is the only one the suite allows)."""
    cfg = Config("alembic.ini", output_buffer=io.StringIO())
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.stamp(cfg, "head", sql=True)
