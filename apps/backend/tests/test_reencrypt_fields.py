"""Re-encrypting the values another field-encryption key wrote (the two-key repair).

A server that once ran under the wrong key leaves values only that key reads,
and the server then refuses to start, naming them. Given the other key,
`FieldEncryptionCoreService.reencrypt_from` (run by
`python -m app.scripts.reencrypt_fields`) re-encrypts exactly those under the
configured key, so the server starts with no row removed. It judges every value
before writing anything, refuses while one decrypts under neither key, and
leaves the values the configured key reads byte for byte.
"""

from __future__ import annotations

import io
import json
from uuid import UUID, uuid4

import pytest
from app.core.exceptions import (
    UnreadableEncryptedValuesException,
    ValidationException,
)
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from app.scripts import reencrypt_fields
from app.services.core.field_encryption_core_service import FieldEncryptionCoreService
from cryptography.fernet import Fernet
from fixtures.factories import make_agent, make_check
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


def _token(key: str | bytes, plaintext: str) -> str:
    raw = key.encode() if isinstance(key, str) else key
    return Fernet(raw).encrypt(plaintext.encode()).decode()


async def _check(db: AsyncSession, **raw: str | None) -> UUID:
    """A check (written through the ORM, so under the configured key) whose
    columns named in ``raw`` then hold exactly the text given."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        check_type="postgres",
        target="https://ours.test",
        check_config={"query": "SELECT 1"},
    )
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


async def _raw(db: AsyncSession, check_id: UUID) -> tuple[str | None, ...]:
    row = (
        await db.execute(
            text(
                "SELECT target, check_config, connection_string_encrypted "
                "FROM checks WHERE id = :id"
            ),
            {"id": check_id},
        )
    ).one()
    return tuple(row)


async def test_the_other_keys_values_are_reencrypted_and_the_server_then_starts(
    db: AsyncSession, field_key: str
) -> None:
    other = Fernet.generate_key().decode()
    config = json.dumps({"query": "SELECT 2", "password": "hunter2"})
    check_id = await _check(
        db,
        check_config=_token(other, config),
        connection_string_encrypted="postgresql://u:hunter2@db/app",  # plaintext
    )
    params = json.dumps({"check_ids": [str(uuid4())]})
    job_id = await _job(db, _token(other, params))
    target_before = (await _raw(db, check_id))[0]
    with pytest.raises(UnreadableEncryptedValuesException):
        await FieldEncryptionCoreService.require_readable(db)

    report = await FieldEncryptionCoreService.reencrypt_from(db, other)
    db.expire_all()

    await FieldEncryptionCoreService.require_readable(db)  # the server starts
    assert sorted(report.reencrypted) == sorted(
        [
            f"checks.check_config id={check_id}",
            f"maintenance_jobs.params id={job_id}",
        ]
    )
    assert report.encrypted == [f"checks.connection_string_encrypted id={check_id}"]
    assert report.left_too_long == []
    target, stored_config, stored_conn = await _raw(db, check_id)
    assert target == target_before  # the configured key's value, byte for byte
    assert stored_config is not None and stored_conn is not None
    assert "hunter2" not in stored_config + stored_conn
    check = (await db.scalars(select(Check).where(Check.id == check_id))).one()
    assert check.check_config == json.loads(config)
    assert check.connection_string_encrypted == "postgresql://u:hunter2@db/app"
    job = (
        await db.scalars(select(MaintenanceJob).where(MaintenanceJob.id == job_id))
    ).one()
    assert job.params == json.loads(params)


async def test_a_value_neither_key_reads_refuses_and_changes_nothing(
    db: AsyncSession, field_key: str
) -> None:
    other = Fernet.generate_key().decode()
    lost = Fernet.generate_key()
    ours = await _check(db, check_config=_token(other, "{}"))
    theirs = await _check(db, target=_token(lost, "https://lost.test"))
    before = (await _raw(db, ours), await _raw(db, theirs))

    with pytest.raises(UnreadableEncryptedValuesException) as refused:
        await FieldEncryptionCoreService.reencrypt_from(db, other)

    assert f"checks.target id={theirs}" in refused.value.message
    assert f"checks.check_config id={ours}" not in refused.value.message
    assert (await _raw(db, ours), await _raw(db, theirs)) == before


@pytest.mark.parametrize(
    "other",
    ["", "not a key", "A" * 43, "  \n", "gAAAAAB" + "a" * 60],
)
async def test_a_value_that_is_no_fernet_key_is_refused(
    db: AsyncSession, field_key: str, other: str
) -> None:
    with pytest.raises(ValidationException, match="not a Fernet key"):
        await FieldEncryptionCoreService.reencrypt_from(db, other)


async def test_the_configured_key_itself_is_refused_even_padded(
    db: AsyncSession, field_key: str
) -> None:
    with pytest.raises(ValidationException, match="SECURITY__FIELD_ENCRYPTION_KEY"):
        await FieldEncryptionCoreService.reencrypt_from(db, f"  {field_key}\n")


async def test_a_second_run_writes_nothing(db: AsyncSession, field_key: str) -> None:
    other = Fernet.generate_key().decode()
    check_id = await _check(
        db, check_config=_token(other, "{}"), connection_string_encrypted="plain"
    )

    await FieldEncryptionCoreService.reencrypt_from(db, other)
    first = await _raw(db, check_id)
    report = await FieldEncryptionCoreService.reencrypt_from(db, other)

    assert await _raw(db, check_id) == first
    assert report.reencrypted == [] and report.encrypted == []
    assert report.already_readable == 3


async def test_a_plaintext_value_too_long_once_encrypted_is_left_and_named(
    db: AsyncSession, field_key: str
) -> None:
    other = Fernet.generate_key().decode()
    long_target = "https://example.com/" + "a" * 900
    check_id = await _check(db, target=long_target)

    report = await FieldEncryptionCoreService.reencrypt_from(db, other)

    assert report.left_too_long == [f"checks.target id={check_id}"]
    assert (await _raw(db, check_id))[0] == long_target


async def test_the_command_commits_its_repair(
    worker_db: async_sessionmaker[AsyncSession],
    field_key: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = Fernet.generate_key().decode()
    async with worker_db() as s:
        check_id = await _check(s, check_config=_token(other, '{"a": 1}'))
        await s.commit()

    assert await reencrypt_fields.reencrypt(other) == 0

    async with worker_db() as fresh:
        await FieldEncryptionCoreService.require_readable(fresh)
        check = (await fresh.scalars(select(Check).where(Check.id == check_id))).one()
        assert check.check_config == {"a": 1}
        await fresh.execute(delete(Check).where(Check.id == check_id))
        await fresh.commit()
    out = capsys.readouterr().out
    assert f"re-encrypted checks.check_config id={check_id}" in out
    assert other not in out


async def test_the_command_refuses_and_exits_1(
    worker_db: async_sessionmaker[AsyncSession],
    field_key: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with worker_db() as s:
        check_id = await _check(s, check_config=_token(Fernet.generate_key(), "{}"))
        await s.commit()

    assert await reencrypt_fields.reencrypt(Fernet.generate_key().decode()) == 1

    err = capsys.readouterr().err
    assert f"checks.check_config id={check_id}" in err
    assert "Nothing was changed" in err


def test_no_key_on_standard_input_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert reencrypt_fields.main(io.StringIO("\n")) == 1
    assert "no key on standard input" in capsys.readouterr().err


def _wrapped(token: str) -> str:
    return "\n".join(token[i : i + 76] for i in range(0, len(token), 76))


async def test_a_wrapped_token_either_key_reads_is_judged_by_its_key(
    db: AsyncSession, field_key: str
) -> None:
    """A token stored line-wrapped or quoted still decrypts (Fernet skips what is
    not base64): one under the configured key is left, one under the other key is
    re-encrypted, and neither is taken for plaintext and encrypted a second time."""
    other = Fernet.generate_key().decode()
    ours = _wrapped(_token(field_key, "https://ours.test/x"))
    theirs = json.dumps(_token(other, '{"password": "hunter2"}'))
    check_id = await _check(db, target=ours, check_config=theirs)

    report = await FieldEncryptionCoreService.reencrypt_from(db, other)
    db.expire_all()

    assert report.encrypted == []
    assert report.reencrypted == [f"checks.check_config id={check_id}"]
    assert (await _raw(db, check_id))[0] == ours
    check = (await db.scalars(select(Check).where(Check.id == check_id))).one()
    assert check.target == "https://ours.test/x"
    assert check.check_config == {"password": "hunter2"}


async def test_a_column_not_yet_text_is_left_for_its_migration(
    db: AsyncSession, field_key: str
) -> None:
    """Before revision 011 maintenance_jobs.params is json, and the startup refusal
    still sends the operator here: the repair leaves that column to 011 (which
    encrypts it) rather than write a token into json, and repairs the rest."""
    other = Fernet.generate_key().decode()
    check_id = await _check(db, target=_token(other, "https://t"))
    await _job(db, '{"check_ids": []}')
    await db.execute(
        text(
            "ALTER TABLE maintenance_jobs ALTER COLUMN params TYPE json "
            "USING params::json"
        )
    )

    report = await FieldEncryptionCoreService.reencrypt_from(db, other)

    assert report.reencrypted == [f"checks.target id={check_id}"]
    assert report.encrypted == []
