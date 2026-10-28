"""Import and bulk-create jobs against what an attacker or a bad file sends.

Through the real worker: a kept error never repeats a secret the input carried;
one bad entry is that entry's error and the rest commit; a name met twice is one
check; an agent that takes no checks keeps the ones it has; the same request
sent twice is one job; an alert deleted since queueing is reported, not fatal.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.core.datetime_utils import utc_now
from app.models.agent_model import Agent
from app.models.alert_model import Alert
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.check_schema import BulkCheckCreateRequest
from app.services.core.check_transfer_core_service import CheckTransferCoreService
from fixtures.factories import make_agent, make_alert, make_check, make_user
from sqlalchemy import select, text

pytestmark = pytest.mark.integration

TOKEN = "TOPSECRETTOKEN9876"


async def _owner(maker, **fields: object) -> UUID:
    user = make_user(**fields)
    async with maker() as s:
        s.add(user)
        await s.commit()
        return user.id


async def _agent(maker, **fields: object) -> UUID:
    agent = make_agent(**fields)
    async with maker() as s:
        s.add(agent)
        await s.commit()
        return agent.id


async def _queue(maker, kind, params, owner_id: UUID | None) -> UUID:
    job = MaintenanceJob(
        id=uuid4(),
        kind=kind,
        target_id=None,
        params=params,
        owner_id=owner_id,
        status=MaintenanceJobStatus.QUEUED,
    )
    async with maker() as s:
        s.add(job)
        await s.commit()
    return job.id


async def _row(maker, job_id: UUID) -> MaintenanceJob:
    async with maker() as s:
        return (
            await s.execute(select(MaintenanceJob).where(MaintenanceJob.id == job_id))
        ).scalar_one()


async def _checks(maker, agent_id: UUID) -> list[Check]:
    async with maker() as s:
        return list(await s.scalars(select(Check).where(Check.agent_id == agent_id)))


async def _run() -> None:
    register_all()
    assert await process_next_job()


# ── Secrets in kept errors ─────────────────────────────────────────────────


async def test_import_error_does_not_leak_a_query_token(worker_db) -> None:
    """A target over 512 characters: the validation error would quote its tail."""
    agent_id = await _agent(worker_db)
    target = "https://hook.example.test/" + "a" * 500 + f"?token={TOKEN}"
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(agent_id),
            "mode": "merge",
            "checks": [{"name": "hook", "check_type": "http", "target": target}],
        },
        await _owner(worker_db, role="admin"),
    )
    await _run()
    row = await _row(worker_db, job_id)
    blob = json.dumps(row.progress) + (row.error or "")
    assert (row.progress or {}).get("failure_count") == 1, blob
    assert TOKEN not in blob, blob


async def test_import_error_does_not_leak_a_non_string_target(worker_db) -> None:
    """A target that is not a string: the validation error would quote it whole."""
    agent_id = await _agent(worker_db)
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(agent_id),
            "mode": "merge",
            "checks": [
                {
                    "name": "hook",
                    "check_type": "http",
                    "target": [f"https://hook.example.test/?token={TOKEN}"],
                }
            ],
        },
        await _owner(worker_db, role="admin"),
    )
    await _run()
    row = await _row(worker_db, job_id)
    blob = json.dumps(row.progress) + (row.error or "")
    assert TOKEN not in blob, blob


async def test_control_import_error_masks_a_userinfo_password(worker_db) -> None:
    agent_id = await _agent(worker_db)
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(agent_id),
            "mode": "merge",
            "checks": [
                {
                    "name": "hook",
                    "check_type": "http",
                    "target": [f"https://u:{TOKEN}@hook.example.test/"],
                }
            ],
        },
        await _owner(worker_db, role="admin"),
    )
    await _run()
    row = await _row(worker_db, job_id)
    assert TOKEN not in json.dumps(row.progress) + (row.error or "")


async def test_bulk_create_error_does_not_leak_a_query_token(worker_db) -> None:
    """A scheme-less URL of 512 characters: with `https://` it is over the
    target limit, and the validation error would quote its tail."""
    agent_id = await _agent(worker_db)
    tail = f"?token={TOKEN}"
    url = "hook.example.test/" + "a" * (512 - 18 - len(tail)) + tail
    assert len(url) == 512
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_CREATE,
        {"agent_id": str(agent_id), "requests": [{"url": url}]},
        await _owner(worker_db, role="editor"),
    )
    await _run()
    row = await _row(worker_db, job_id)
    blob = json.dumps(row.progress) + (row.error or "")
    assert (row.progress or {}).get("failure_count") == 1, blob
    assert TOKEN not in blob, blob


# ── One bad entry and the rest ─────────────────────────────────────────────


async def test_one_nul_entry_does_not_sink_the_rest_of_an_import(worker_db) -> None:
    """One entry the database refuses is that entry's error, never the rest's."""
    agent_id = await _agent(worker_db)
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(agent_id),
            "mode": "merge",
            "checks": [
                {"name": "a", "check_type": "http", "target": "https://a.test"},
                {
                    "name": "bad",
                    "check_type": "http",
                    "target": "https://b.test",
                    "description": "x\u0000y",
                },
                {"name": "c", "check_type": "http", "target": "https://c.test"},
            ],
        },
        await _owner(worker_db, role="admin"),
    )
    await _run()
    row = await _row(worker_db, job_id)
    names = sorted(c.display_name for c in await _checks(worker_db, agent_id))
    assert row.status == MaintenanceJobStatus.DONE, (row.error, row.progress)
    assert names == ["a", "c"], (names, row.progress)


async def test_one_nul_url_does_not_sink_the_rest_of_a_bulk_create(worker_db) -> None:
    agent_id = await _agent(worker_db)
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_CREATE,
        {
            "agent_id": str(agent_id),
            "requests": [
                {"url": "https://a.test"},
                {"url": "https://b.test", "display_name": "b\u0000"},
                {"url": "https://c.test"},
            ],
        },
        await _owner(worker_db, role="editor"),
    )
    await _run()
    row = await _row(worker_db, job_id)
    made = await _checks(worker_db, agent_id)
    assert row.status == MaintenanceJobStatus.DONE, (row.error, row.progress)
    assert len(made) == 2, (row.error, row.progress)


async def test_control_a_good_import_is_done(worker_db) -> None:
    agent_id = await _agent(worker_db)
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(agent_id),
            "mode": "merge",
            "checks": [
                {"name": "a", "check_type": "http", "target": "https://a.test"},
                {"name": "c", "check_type": "http", "target": "https://c.test"},
            ],
        },
        await _owner(worker_db, role="admin"),
    )
    await _run()
    row = await _row(worker_db, job_id)
    assert row.status == MaintenanceJobStatus.DONE
    assert len(await _checks(worker_db, agent_id)) == 2


# ── Duplicates, alerts, agent state ────────────────────────────────────────


async def test_merge_import_with_a_name_twice_makes_one_check(worker_db) -> None:
    """`merge` skips a check whose name the agent has, the first `web` among them."""
    agent_id = await _agent(worker_db)
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(agent_id),
            "mode": "merge",
            "checks": [
                {"name": "web", "check_type": "http", "target": "https://a.test"},
                {"name": "web", "check_type": "http", "target": "https://b.test"},
            ],
        },
        await _owner(worker_db, role="admin"),
    )
    await _run()
    row = await _row(worker_db, job_id)
    made = await _checks(worker_db, agent_id)
    assert len(made) == 1, (len(made), row.progress)


async def test_replace_import_onto_archived_agent_changes_nothing(worker_db) -> None:
    """An archived agent takes no checks, and keeps the ones it has as they are."""
    agent_id = await _agent(worker_db, approval_status="archived")
    async with worker_db() as s:
        s.add(
            make_check(
                agent_id=agent_id,
                display_name="web",
                check_type="http",
                target="https://old.test",
            )
        )
        await s.commit()
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(agent_id),
            "mode": "replace",
            "checks": [
                {"name": "web", "check_type": "http", "target": "https://new.test"},
                {"name": "new", "check_type": "http", "target": "https://n.test"},
            ],
        },
        await _owner(worker_db, role="admin"),
    )
    await _run()
    row = await _row(worker_db, job_id)
    (check,) = await _checks(worker_db, agent_id)
    assert check.target == "https://old.test", row.progress


async def test_round_trip_keeps_retry_attempts_zero(worker_db) -> None:
    """An export imported again keeps a retry count of 0."""
    src = await _agent(worker_db)
    dst = await _agent(worker_db)
    async with worker_db() as s:
        s.add(
            make_check(
                agent_id=src,
                display_name="web",
                check_type="http",
                target="https://a.test",
                retry_attempts=0,
            )
        )
        await s.commit()
    async with worker_db() as s:
        doc = await CheckTransferCoreService.export_document(s, src)
    await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(dst),
            "mode": "merge",
            "checks": doc.model_dump(mode="json")["checks"],
        },
        await _owner(worker_db, role="admin"),
    )
    await _run()
    (copy,) = await _checks(worker_db, dst)
    assert copy.retry_attempts == 0, copy.retry_attempts


async def test_a_double_submitted_bulk_create_makes_each_check_once(worker_db) -> None:
    """A double click, or a client's retry, while the first is queued is one job."""
    agent_id = await _agent(worker_db)
    owner_id = await _owner(worker_db, role="editor")
    items = [BulkCheckCreateRequest(url="https://a.test")]
    for _ in range(2):
        async with worker_db() as s:
            await CheckTransferCoreService.enqueue_bulk_create(
                s, owner_id, agent_id, items, []
            )
            await s.commit()
    register_all()
    while await process_next_job():
        pass
    made = await _checks(worker_db, agent_id)
    assert len(made) == 1, [c.display_name for c in made]


async def test_an_alert_deleted_after_enqueue_keeps_the_counts(worker_db) -> None:
    """The checks are made and counted; the vanished alert is that job's error."""
    agent_id = await _agent(worker_db)
    alert = make_alert()
    async with worker_db() as s:
        s.add(alert)
        await s.commit()
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_CREATE,
        {
            "agent_id": str(agent_id),
            "requests": [{"url": "https://a.test"}, {"url": "https://b.test"}],
            "alert_ids": [str(alert.id)],
        },
        await _owner(worker_db, role="editor"),
    )
    async with worker_db() as s:
        row = await s.get(Alert, alert.id)
        row.deleted_at = utc_now()
        await s.commit()
    await _run()
    job = await _row(worker_db, job_id)
    made = await _checks(worker_db, agent_id)
    assert job.status == MaintenanceJobStatus.DONE, job.error
    assert len(made) == 2
    assert job.progress is not None
    assert job.progress["success_count"] == 2
    assert any("no longer exists" in e for e in job.progress["errors"])


async def test_control_bulk_create_onto_an_agent_archived_after_enqueue(
    worker_db,
) -> None:
    agent_id = await _agent(worker_db)
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_CREATE,
        {"agent_id": str(agent_id), "requests": [{"url": "https://a.test"}]},
        await _owner(worker_db, role="editor"),
    )
    async with worker_db() as s:
        (await s.get(Agent, agent_id)).approval_status = "archived"
        await s.commit()
    await _run()
    job = await _row(worker_db, job_id)
    assert job.status == MaintenanceJobStatus.FAILED
    assert (job.progress or {}).get("failure_count") == 1
    assert not await _checks(worker_db, agent_id)


# ── Every type's own settings survive a round trip ─────────────────────────

# A script the synthetic guard passes, as an import validates it, in the form a
# create stores (strings are stripped).
SCRIPT = "async def run_check(page):\n    await page.goto('https://example.invalid')"


def _settings(check: Check) -> dict[str, object]:
    """Everything an import is meant to recreate."""
    return {
        "check_type": check.check_type,
        "target": check.target,
        "retry_interval_seconds": check.retry_interval_seconds,
        "resend_notification_after": check.resend_notification_after,
        "check_config": check.check_config,
        "script_code": check.script_code,
    }


async def _import(maker, dst: UUID, checks: list[dict], mode: str = "merge") -> UUID:
    job_id = await _queue(
        maker,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {"agent_id": str(dst), "mode": mode, "checks": checks},
        await _owner(maker, role="admin"),
    )
    await _run()
    return job_id


async def _export(maker, src: UUID) -> list[dict]:
    async with maker() as s:
        doc = await CheckTransferCoreService.export_document(s, src)
    return doc.model_dump(mode="json")["checks"]


async def test_round_trip_keeps_every_type_s_own_settings(worker_db) -> None:
    """Database, DNS, SSL-skipping, JSON and synthetic checks come back whole."""
    src = await _agent(worker_db)
    dst = await _agent(worker_db)
    async with worker_db() as s:
        s.add_all(
            [
                make_check(
                    agent_id=src,
                    display_name="pg",
                    check_type="postgres",
                    target="db.example.com",
                    check_config={
                        "connection_string": "postgresql://m:pw@db.example.com/app",
                        "query": "SELECT 1",
                    },
                ),
                make_check(
                    agent_id=src,
                    display_name="my",
                    check_type="mysql",
                    target="my.example.com",
                    check_config={
                        "connection_string": "mysql://m:pw@my.example.com/app",
                        "query": "SELECT 2",
                    },
                ),
                make_check(
                    agent_id=src,
                    display_name="dns",
                    check_type="dns",
                    target="example.com",
                    check_config={
                        "record_type": "MX",
                        "nameserver": "1.1.1.1",
                        "port": 5353,
                        "expect_value": "mx.example.com",
                    },
                ),
                make_check(
                    agent_id=src,
                    display_name="web",
                    check_type="http",
                    target="https://a.example.com",
                    retry_interval_seconds=90,
                    resend_notification_after=7,
                    check_config={
                        "http_method": "HEAD",
                        "verify_ssl": False,
                        "expected_status": 204,
                    },
                ),
                make_check(
                    agent_id=src,
                    display_name="api",
                    check_type="json",
                    target="https://api.example.com",
                    check_config={
                        "http_method": "POST",
                        "verify_ssl": False,
                        "expected_status": 200,
                        "json_path": "status",
                        "expected_value": "ok",
                    },
                ),
                make_check(
                    agent_id=src,
                    display_name="browse",
                    check_type="synthetic",
                    target="https://b.example.com",
                    script_code=SCRIPT,
                ),
            ]
        )
        await s.commit()
    job_id = await _import(worker_db, dst, await _export(worker_db, src))

    want = {c.display_name: _settings(c) for c in await _checks(worker_db, src)}
    got = {c.display_name: _settings(c) for c in await _checks(worker_db, dst)}
    progress = (await _row(worker_db, job_id)).progress
    for name, settings in want.items():
        assert got.get(name) == settings, (name, got.get(name), settings, progress)
    assert got.keys() == want.keys()


async def test_round_trip_keeps_dependencies(worker_db) -> None:
    """A parent on the exported agent is the copy's parent on the new one; a
    parent on another agent stays the parent."""
    src = await _agent(worker_db)
    dst = await _agent(worker_db)
    other = await _agent(worker_db)
    far = make_check(agent_id=other, display_name="far")
    near = make_check(agent_id=src, display_name="near")
    async with worker_db() as s:
        s.add_all([far, near])
        await s.flush()
        # Each child sorts before its parent, so the import meets it first.
        s.add_all(
            [
                make_check(
                    agent_id=src, display_name="a-child", depends_on_check_id=near.id
                ),
                make_check(
                    agent_id=src, display_name="b-child", depends_on_check_id=far.id
                ),
            ]
        )
        await s.commit()
    job_id = await _import(worker_db, dst, await _export(worker_db, src))

    copies = {c.display_name: c for c in await _checks(worker_db, dst)}
    progress = (await _row(worker_db, job_id)).progress
    assert copies["a-child"].depends_on_check_id == copies["near"].id, progress
    assert copies["b-child"].depends_on_check_id == far.id, progress


async def test_replace_with_an_older_entry_keeps_what_it_does_not_name(
    worker_db,
) -> None:
    """An entry from an export that predates a field leaves that field alone."""
    dst = await _agent(worker_db)
    async with worker_db() as s:
        s.add(
            make_check(
                agent_id=dst,
                display_name="browse",
                check_type="synthetic",
                target="https://b.example.com",
                script_code=SCRIPT,
                retry_interval_seconds=90,
            )
        )
        await s.commit()
    old_entry = {
        "name": "browse",
        "check_type": "synthetic",
        "target": "https://c.example.com",
    }
    job_id = await _import(worker_db, dst, [old_entry], mode="replace")

    (check,) = await _checks(worker_db, dst)
    assert check.target == "https://c.example.com", (
        await _row(worker_db, job_id)
    ).progress
    assert check.script_code == SCRIPT
    assert check.retry_interval_seconds == 90


async def test_a_parent_name_the_agent_lacks_is_that_entry_s_error(
    worker_db,
) -> None:
    """The check is made; the missing parent is reported, never guessed."""
    dst = await _agent(worker_db)
    entry = {
        "name": "child",
        "check_type": "ping",
        "target": "127.0.0.1",
        "depends_on": "nowhere",
    }
    job_id = await _import(worker_db, dst, [entry])

    (check,) = await _checks(worker_db, dst)
    assert check.depends_on_check_id is None
    progress = (await _row(worker_db, job_id)).progress
    assert "nowhere" in json.dumps(progress), progress


async def test_replace_import_through_the_worker_updates_the_check(worker_db) -> None:
    """The worker's import has listed the agent's checks (and the agent its
    own) before it updates one; the update still finishes."""
    dst = await _agent(worker_db)
    async with worker_db() as s:
        s.add(
            make_check(
                agent_id=dst,
                display_name="web",
                check_type="http",
                target="https://b.example.com",
            )
        )
        await s.commit()
    job_id = await _import(
        worker_db,
        dst,
        [{"name": "web", "check_type": "http", "target": "https://c.example.com"}],
        mode="replace",
    )

    progress = (await _row(worker_db, job_id)).progress
    assert progress and progress["updated"] == 1, progress
    (check,) = await _checks(worker_db, dst)
    assert check.target == "https://c.example.com"


async def test_a_queued_import_keeps_its_secrets_encrypted_at_rest(
    worker_db, field_key
) -> None:
    """An import's entries wait in the job row with their connection strings
    and scripts: the row holds them encrypted, and the worker still reads them."""
    dst = await _agent(worker_db)
    secret = "postgresql://m:hunter2@db.example.com/app"
    job_id = await _import(
        worker_db,
        dst,
        [
            {
                "name": "pg",
                "check_type": "postgres",
                "target": "db.example.com",
                "connection_string": secret,
                "query": "SELECT 1",
            }
        ],
    )

    async with worker_db() as s:
        stored = await s.scalar(
            text("SELECT params::text FROM maintenance_jobs WHERE id = :id"),
            {"id": job_id},
        )
    assert stored and "hunter2" not in stored, stored
    (check,) = await _checks(worker_db, dst)
    assert check.connection_string == secret
