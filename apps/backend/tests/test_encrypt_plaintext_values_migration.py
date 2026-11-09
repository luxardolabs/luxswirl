"""Migration 018 encrypts every encrypted column's value still stored in the clear.

A check's target, check_config and connection string, and a maintenance job's
params, stored before their column was encrypted are read as they are, so their
secrets stayed in plaintext until the row was next saved. The revision encrypts
each such value under the server's key, byte for byte: it reads back exactly as
it read before. A value already shaped as a token (whichever key wrote it) is
never touched, and a second run changes nothing.

The suite's schema is already at head, so this runs the revision's `migrate`
directly against rows shaped like an older version's.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from types import ModuleType
from uuid import UUID, uuid4

import pytest
from app.core.config import settings
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from cryptography.fernet import Fernet
from fixtures.factories import make_agent, make_check
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "018_encrypt_plaintext_values.py"
)

_CHECK_COLUMNS = ("target", "check_config", "connection_string_encrypted")


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rev018", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _migrate(db: AsyncSession) -> None:
    revision = _revision()
    await db.run_sync(lambda session: revision.migrate(session.connection()))
    db.expire_all()


async def _check(db: AsyncSession, **raw: str | None) -> UUID:
    """A check whose encrypted columns hold exactly ``raw`` (column: stored text)."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, check_type="http", target="x")
    db.add(check)
    await db.flush()
    for column, value in raw.items():
        await db.execute(
            text(f"UPDATE checks SET {column} = :raw WHERE id = :id"),
            {"raw": value, "id": check.id},
        )
    return check.id


async def _job(db: AsyncSession, raw: str) -> UUID:
    job = MaintenanceJob(
        id=uuid4(),
        kind=MaintenanceJobKind.BULK_CHECK_DELETE,
        target_id=uuid4(),
        params={"check_ids": []},
        status=MaintenanceJobStatus.QUEUED,
    )
    db.add(job)
    await db.flush()
    await db.execute(
        text("UPDATE maintenance_jobs SET params = :raw WHERE id = :id"),
        {"raw": raw, "id": job.id},
    )
    return job.id


async def _raw_check(db: AsyncSession, check_id: UUID) -> dict[str, str | None]:
    row = (
        await db.execute(
            text(
                "SELECT target, check_config, connection_string_encrypted "
                "FROM checks WHERE id = :id"
            ),
            {"id": check_id},
        )
    ).one()
    return dict(zip(_CHECK_COLUMNS, row, strict=True))


async def _raw_job(db: AsyncSession, job_id: UUID) -> str:
    return await db.scalar(
        text("SELECT params FROM maintenance_jobs WHERE id = :id"), {"id": job_id}
    )


def _opens(key: str, stored: str) -> str:
    return Fernet(key.encode()).decrypt(stored.encode()).decode()


async def test_plaintext_check_columns_are_encrypted_and_read_the_same(
    db: AsyncSession, field_key: str
) -> None:
    plain = {
        "target": "https://admin:hunter2@example.com/health",
        "check_config": json.dumps({"headers": {"Authorization": "Bearer s3cret"}}),
        "connection_string_encrypted": "postgresql://u:hunter2@db/app",
    }
    check_id = await _check(db, **plain)
    before = (await db.scalars(select(Check).where(Check.id == check_id))).one()
    seen = (before.target, before.check_config, before.connection_string_encrypted)
    db.expire_all()

    await _migrate(db)

    stored = await _raw_check(db, check_id)
    for column, value in plain.items():
        token = stored[column]
        assert token is not None
        assert "hunter2" not in token and "s3cret" not in token
        assert _opens(field_key, token) == value  # byte for byte
    after = (await db.scalars(select(Check).where(Check.id == check_id))).one()
    assert (after.target, after.check_config, after.connection_string_encrypted) == seen


async def test_plaintext_job_params_are_encrypted(
    db: AsyncSession, field_key: str
) -> None:
    plain = json.dumps({"entries": [{"target": "mysql://u:hunter2@db/x"}]})
    job_id = await _job(db, plain)

    await _migrate(db)

    token = await _raw_job(db, job_id)
    assert "hunter2" not in token
    assert _opens(field_key, token) == plain
    job = (
        await db.scalars(select(MaintenanceJob).where(MaintenanceJob.id == job_id))
    ).one()
    assert job.params == json.loads(plain)


async def test_tokens_under_any_key_are_left_byte_for_byte(
    db: AsyncSession, field_key: str
) -> None:
    """Never encrypt a token again: one under the server's key, and one under
    another key (which only the operator's repair can judge), stay as stored."""
    ours = Fernet(field_key.encode()).encrypt(b"https://ours.test").decode()
    theirs = Fernet(Fernet.generate_key()).encrypt(b'{"a": 1}').decode()
    check_id = await _check(
        db, target=ours, check_config=theirs, connection_string_encrypted=None
    )

    await _migrate(db)

    assert await _raw_check(db, check_id) == {
        "target": ours,
        "check_config": theirs,
        "connection_string_encrypted": None,
    }


async def test_a_second_run_changes_nothing(db: AsyncSession, field_key: str) -> None:
    check_id = await _check(db, target="https://plain.test", check_config="{}")
    job_id = await _job(db, "{}")

    await _migrate(db)
    first = (await _raw_check(db, check_id), await _raw_job(db, job_id))
    await _migrate(db)

    assert (await _raw_check(db, check_id), await _raw_job(db, job_id)) == first


async def test_plaintext_that_is_not_a_json_container_still_reads_the_same(
    db: AsyncSession, field_key: str
) -> None:
    """A check_config that reads as nothing before (bare JSON, invalid JSON)
    reads as nothing after: the stored text is encrypted as it is, never
    rewritten as another value."""
    odd = ['"a string"', "not json", "null", ""]
    ids = [await _check(db, check_config=value) for value in odd]

    await _migrate(db)

    for check_id, value in zip(ids, odd, strict=True):
        token = (await _raw_check(db, check_id))["check_config"]
        assert token is not None and _opens(field_key, token) == value
        check = (await db.scalars(select(Check).where(Check.id == check_id))).one()
        assert check.check_config is None


async def test_a_value_too_long_once_encrypted_is_left_and_named(
    db: AsyncSession, field_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """target is varchar(1000): a long plaintext whose token would not fit is
    left as it is and named, and the upgrade goes on with the other rows."""
    long_target = "https://example.com/" + "a" * 900
    long_id = await _check(db, target=long_target)
    short_id = await _check(db, target="https://short.test")

    with caplog.at_level(logging.WARNING, logger="alembic.runtime.migration"):
        await _migrate(db)

    assert (await _raw_check(db, long_id))["target"] == long_target
    assert f"checks.target id={long_id}" in caplog.text
    assert _opens(field_key, (await _raw_check(db, short_id))["target"] or "") == (
        "https://short.test"
    )


async def test_without_a_key_a_plaintext_value_fails_the_upgrade(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _check(db, target="https://plain.test")
    monkeypatch.setattr(settings.security, "field_encryption_key", "")

    with pytest.raises(RuntimeError, match="SECURITY__FIELD_ENCRYPTION_KEY"):
        await _migrate(db)
