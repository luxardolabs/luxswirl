"""The server and its migrations refuse a field-encryption key that cannot
read every stored encrypted value, and name each value it cannot read.

A server running under the wrong SECURITY__FIELD_ENCRYPTION_KEY fails every
read of a check and writes rows the right key then refuses, each one taking
down every unfiltered check list. So every check's encrypted columns and every
maintenance job's params are read as stored, before migrations run and before
the server starts, whatever else the data holds.
"""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path
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

_REVISION_016 = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "016_closed_set_encrypted_configs.py"
)


def _use_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setattr(settings.security, "field_encryption_key", key)


def _another_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_key(monkeypatch, Fernet.generate_key().decode())


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


async def _store(db: AsyncSession, check_id: UUID, column: str, raw: str) -> None:
    """Store ``raw`` as the column's value, as a value stored before encryption."""
    await db.execute(
        text(f"UPDATE checks SET {column} = :raw WHERE id = :id"),
        {"raw": raw, "id": check_id},
    )


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
    await _job(db, MaintenanceJobStatus.DONE)

    await FieldEncryptionCoreService.require_readable(db)


async def test_another_key_is_refused_naming_every_value(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = await _agent(db)
    check_id = await _check(db, agent_id)
    job_id = await _job(db, MaintenanceJobStatus.QUEUED)
    _another_key(monkeypatch)

    message = await _refusal(db)

    assert "is not this database's key" in message
    assert "none of the 4 stored encrypted values" in message
    for column in ("target", "check_config", "connection_string_encrypted"):
        assert f"checks.{column} id={check_id}" in message
    assert f"maintenance_jobs.params id={job_id}" in message
    assert "docs/guides/troubleshooting.md" in message


async def test_a_value_stored_before_encryption_does_not_vouch_for_a_wrong_key(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plaintext value reads under any key, so it says nothing about the
    key: a wholly wrong key gets the verdict that removes nothing, never "N do
    not decrypt (M do)", whose repair is deleting every named check."""
    agent_id = await _agent(db)
    await _check(db, agent_id)
    legacy = await _check(db, agent_id)
    await _store(db, legacy, "target", "https://example.com")
    _another_key(monkeypatch)

    message = await _refusal(db)

    assert "is not this database's key" in message
    assert "none of the 5 stored encrypted values" in message


async def test_two_keys_are_named_without_choosing_one_or_removing_rows(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two keys wrote this database: each one names exactly what it cannot
    read, with both counts, and neither is told it is wrong or to remove rows.
    Which key is right is the operator's to say: a count cannot, whichever
    key wrote more."""
    agent_id = await _agent(db)
    good = [await _check(db, agent_id) for _ in range(3)]
    wrong = Fernet.generate_key().decode()
    _use_key(monkeypatch, wrong)
    stray = await _check(db, agent_id)

    for key, decrypted, named, unnamed in (
        (wrong, 3, good, [stray]),
        (field_key, 9, [stray], good),
    ):
        _use_key(monkeypatch, key)
        message = await _refusal(db)

        assert message.startswith(
            f"{3 * len(named)} stored encrypted values do not decrypt under "
            f"SECURITY__FIELD_ENCRYPTION_KEY ({decrypted} do)"
        )
        assert "Two keys wrote this database" in message
        assert "app.scripts.reencrypt_fields" in message
        assert "not this database's key" not in message
        assert "remove" not in message.lower()
        assert all(f"id={check_id}" in message for check_id in named)
        assert not any(f"id={check_id}" in message for check_id in unnamed)


async def test_a_check_edited_under_another_key_is_named_for_that_column_alone(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit re-encrypts only the column it changes: the other columns stay
    under the right key, so only the edited column is named."""
    agent_id = await _agent(db)
    check_id = await _check(db, agent_id)
    other = Fernet(Fernet.generate_key())
    await _store(
        db, check_id, "check_config", other.encrypt(b'{"api_key": "x"}').decode()
    )

    message = await _refusal(db)

    assert f"checks.check_config id={check_id}" in message
    assert f"checks.target id={check_id}" not in message
    assert f"checks.connection_string_encrypted id={check_id}" not in message


async def test_params_still_typed_json_before_revision_011_are_judged(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revisions 000-010 leave maintenance_jobs.params typed json, and the
    migration check runs on such a database: its values are judged as text,
    plaintext passing under the right key and a token naming a wrong one."""
    await db.execute(
        text(
            "ALTER TABLE maintenance_jobs ALTER COLUMN params TYPE json "
            "USING params::json"
        )
    )
    plain = uuid4()
    await db.execute(
        text(
            "INSERT INTO maintenance_jobs "
            "(id, kind, target_id, params, status, created_at) VALUES "
            "(:id, 'bulk_check_delete', :target, CAST(:params AS json), 'done', now())"
        ),
        {"id": plain, "target": uuid4(), "params": '{"check_ids": []}'},
    )

    await FieldEncryptionCoreService.require_readable_before_migrating(db)

    agent_id = await _agent(db)
    await _check(db, agent_id)
    _another_key(monkeypatch)

    message = await _refusal(db)

    assert "is not this database's key" in message
    assert f"id={plain}" not in message


async def test_a_value_stored_before_encryption_still_starts(
    db: AsyncSession, field_key: str
) -> None:
    agent_id = await _agent(db)
    check_id = await _check(db, agent_id)
    await _store(db, check_id, "target", "https://example.com")

    await FieldEncryptionCoreService.require_readable(db)


async def test_finished_jobs_alone_still_identify_the_key(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no checks, a finished job's params are the only evidence of the
    key: a wrong key that skipped them would start and write under itself."""
    done = await _job(db, MaintenanceJobStatus.DONE)
    failed = await _job(db, MaintenanceJobStatus.FAILED)
    _another_key(monkeypatch)

    message = await _refusal(db)

    assert "is not this database's key" in message
    assert f"maintenance_jobs.params id={done}" in message
    assert f"maintenance_jobs.params id={failed}" in message


async def test_more_than_twenty_values_are_counted_not_all_named(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = await _agent(db)
    for _ in range(8):
        await _check(db, agent_id)
    _another_key(monkeypatch)

    message = await _refusal(db)

    assert message.count(" id=") == 20
    assert ", and 4 more." in message


async def test_every_row_is_judged_across_page_boundaries(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More checks than one read takes: the count shows every value on every
    page was judged, and the read ends."""
    agent_id = await _agent(db)
    db.add_all(
        make_check(agent_id=agent_id, target=f"https://u:p@host{n}.example.com/")
        for n in range(1001)
    )
    await db.flush()
    _another_key(monkeypatch)

    message = await _refusal(db)

    assert "none of the 1001 stored encrypted values" in message
    assert ", and 981 more." in message


async def test_a_migration_run_under_another_key_is_refused_before_it_writes(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrations run before the server starts, and revision 016 rewrites a
    plaintext config it fixes under the key configured now. The check alembic's
    env.py runs first refuses that key, and the config stays as stored."""
    agent_id = await _agent(db)
    await _check(db, agent_id)
    legacy = await _check(db, agent_id)
    await _store(db, legacy, "check_config", '{"http_method": "get"}')
    _another_key(monkeypatch)

    with pytest.raises(UnreadableEncryptedValuesException):
        await FieldEncryptionCoreService.require_readable_before_migrating(db)

    spec = importlib.util.spec_from_file_location("rev016", _REVISION_016)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    _use_key(monkeypatch, field_key)
    await FieldEncryptionCoreService.require_readable_before_migrating(db)
    await db.run_sync(lambda session: revision.migrate(session.connection()))
    db.expire_all()
    await FieldEncryptionCoreService.require_readable(db)


async def test_the_migration_check_passes_the_right_key(
    db: AsyncSession, field_key: str
) -> None:
    agent_id = await _agent(db)
    await _check(db, agent_id)

    await FieldEncryptionCoreService.require_readable_before_migrating(db)


async def test_the_server_stops_at_startup_with_no_agent_seen(
    db: AsyncSession, field_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifespan refuses before it reads anything else, whether or not an
    agent was seen in the last ten minutes."""
    agent_id = await _agent(db, last_seen=utc_now() - timedelta(hours=1))
    await _check(db, agent_id)
    _another_key(monkeypatch)

    async def _no_timescale() -> None:
        return None

    monkeypatch.setattr(main, "init_db", _no_timescale)

    with pytest.raises(UnreadableEncryptedValuesException):
        async with main.lifespan(main.app):
            pass
