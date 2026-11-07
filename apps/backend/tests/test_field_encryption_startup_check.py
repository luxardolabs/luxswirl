"""The server refuses to start under a field-encryption key that cannot read
every stored encrypted value, and names each value it cannot read.

A wrong SECURITY__FIELD_ENCRYPTION_KEY used to boot or crash depending on
whether an agent had been seen in the last ten minutes, and once booted it
wrote rows the right key then refused, each one taking down every unfiltered
check list. The startup check reads every check's encrypted columns and every
unfinished maintenance job's params as stored, before anything else runs.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app import main
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import UnreadableEncryptedValuesException
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.field_encryption_core_service import (
    FieldEncryptionCoreService,
)
from cryptography.fernet import Fernet
from fixtures.factories import make_agent, make_check
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


def _use_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setattr(settings.security, "field_encryption_key", key)


async def _agent(db: AsyncSession, **overrides: object) -> UUID:
    agent = make_agent(**overrides)
    db.add(agent)
    await db.flush()
    return agent.id


async def _check(db: AsyncSession, agent_id: UUID) -> UUID:
    check = make_check(
        agent_id=agent_id,
        target="https://u:hunter2@example.com/health",
        check_config={"api_key": "hunter2"},
        connection_string_encrypted="postgresql://u:hunter2@db/app",
    )
    db.add(check)
    await db.flush()
    return check.id


async def _job(db: AsyncSession, status: MaintenanceJobStatus) -> UUID:
    job = MaintenanceJob(
        id=uuid4(),
        kind=MaintenanceJobKind.BULK_CHECK_DELETE,
        target_id=uuid4(),
        params={"check_ids": [str(uuid4())]},
        status=status,
    )
    db.add(job)
    await db.flush()
    return job.id


async def _refusal(db: AsyncSession) -> str:
    with pytest.raises(UnreadableEncryptedValuesException) as refused:
        await FieldEncryptionCoreService.require_readable(db)
    return refused.value.message


async def test_the_key_that_wrote_every_value_starts(
    db: AsyncSession, field_key: str
) -> None:
    agent_id = await _agent(db)
    await _check(db, agent_id)
    await _job(db, MaintenanceJobStatus.QUEUED)

    await FieldEncryptionCoreService.require_readable(db)


async def test_another_key_is_refused_naming_every_value(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = await _agent(db)
    check_id = await _check(db, agent_id)
    job_id = await _job(db, MaintenanceJobStatus.QUEUED)
    _use_key(monkeypatch, Fernet.generate_key().decode())

    message = await _refusal(db)

    assert "is not this database's key" in message
    assert "none of the 4 stored encrypted values" in message
    for column in ("target", "check_config", "connection_string_encrypted"):
        assert f"checks.{column} id={check_id}" in message
    assert f"maintenance_jobs.params id={job_id}" in message
    assert "docs/guides/troubleshooting.md" in message


async def test_a_value_another_key_wrote_is_named_alone(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row written while another key was set: the right key reads the rest
    and names that row alone."""
    agent_id = await _agent(db)
    kept = await _check(db, agent_id)
    _use_key(monkeypatch, Fernet.generate_key().decode())
    stray = await _check(db, agent_id)
    _use_key(monkeypatch, field_key)

    message = await _refusal(db)

    assert message.startswith(
        "3 stored encrypted values do not decrypt under "
        "SECURITY__FIELD_ENCRYPTION_KEY (3 do)"
    )
    assert f"id={stray}" in message
    assert f"id={kept}" not in message


async def test_a_value_stored_before_encryption_still_starts(
    db: AsyncSession, field_key: str
) -> None:
    agent_id = await _agent(db)
    check_id = await _check(db, agent_id)
    await db.execute(
        text("UPDATE checks SET target = 'https://example.com' WHERE id = :id"),
        {"id": check_id},
    )

    await FieldEncryptionCoreService.require_readable(db)


async def test_a_finished_jobs_params_are_not_judged(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished job's params are never read again, so a key that cannot read
    them does not stop the server."""
    _use_key(monkeypatch, Fernet.generate_key().decode())
    for status in (
        MaintenanceJobStatus.DONE,
        MaintenanceJobStatus.FAILED,
    ):
        await _job(db, status)
    _use_key(monkeypatch, field_key)

    await FieldEncryptionCoreService.require_readable(db)


async def test_more_than_twenty_values_are_counted_not_all_named(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = await _agent(db)
    for _ in range(8):
        await _check(db, agent_id)
    _use_key(monkeypatch, Fernet.generate_key().decode())

    message = await _refusal(db)

    assert message.count(" id=") == 20
    assert ", and 4 more." in message


async def test_the_server_stops_at_startup_with_no_agent_seen(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifespan refuses before it reads anything else, so the outcome no
    longer depends on whether an agent was seen in the last ten minutes."""
    agent_id = await _agent(db, last_seen=utc_now() - timedelta(hours=1))
    await _check(db, agent_id)
    _use_key(monkeypatch, Fernet.generate_key().decode())

    async def _no_timescale() -> None:
        return None

    monkeypatch.setattr(main, "init_db", _no_timescale)

    with pytest.raises(UnreadableEncryptedValuesException):
        async with main.lifespan(main.app):
            pass
