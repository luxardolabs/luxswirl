"""A request's wait for the per-agent advisory locks (the synthetic guard,
the check-names hold) is bounded below its statement timeout.

A guard refused part-way through several agents leaves none held and the
transaction usable; a granted one stays held and lock_timeout is reset; one
taken in a savepoint that rolls back is released; a maintenance job (no
statement timeout) waits past the request's bound. A request beside an import
that holds the agent's names (a scan quick-create) or its guard (an edit) is
answered busy before its statement timeout.
"""

from __future__ import annotations

import asyncio
import socket
import time
from uuid import UUID

import pytest
from app.crud.check_crud import CheckCRUD
from app.models.check_model import Check
from app.schemas.check_schema import CheckUpdate
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_transfer_core_service import CheckTransferCoreService
from app.services.core.job_to_check_core_service import (
    JobToCheckCoreService,
    QuickCheckParams,
)
from fixtures.factories import make_agent, make_check
from sqlalchemy import select, text

pytestmark = pytest.mark.integration

TARGET = "https://127.0.0.1/"
SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://127.0.0.1/')\n"
    "    return {'status': 'success', 'steps': ['x']}\n"
)
FINISH = 30


async def _agent(worker_db) -> UUID:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.commit()
        return agent.id


async def _check(
    worker_db, agent_id: UUID, name: str, check_type: str = "http"
) -> UUID:
    async with worker_db() as s:
        check = make_check(
            agent_id=agent_id,
            display_name=name,
            check_type=check_type,
            target=TARGET,
            **({"script_code": SCRIPT} if check_type == "synthetic" else {}),
        )
        s.add(check)
        await s.commit()
        return check.id


async def _hold_for(
    worker_db, agents: set[UUID], seconds: float, ready: asyncio.Event
) -> None:
    async with worker_db() as s:
        await s.execute(text("SET LOCAL statement_timeout = 0"))
        assert await CheckCRUD.hold_synthetic_guard(s, agents)
        ready.set()
        await asyncio.sleep(seconds)
        await s.rollback()


async def _advisory_held_by_others(worker_db) -> int:
    async with worker_db() as s:
        return int(
            await s.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                    " AND classid = 1826301560 AND pid <> pg_backend_pid()"
                )
            )
            or 0
        )


async def test_partial_refusal_leaves_no_guard_and_a_usable_transaction(worker_db):
    """Free agent sorts first, busy agent second: the refusal must release the
    first agent's guard, reset lock_timeout and leave the session writable."""
    a, b = await _agent(worker_db), await _agent(worker_db)
    free, busy = sorted([a, b])
    plain = await _check(worker_db, free, "plain")
    ready = asyncio.Event()
    holder = asyncio.create_task(_hold_for(worker_db, {busy}, 8, ready))
    await ready.wait()
    async with worker_db() as s:
        started = time.monotonic()
        got = await CheckCRUD.hold_synthetic_guard(s, {free, busy})
        waited = time.monotonic() - started
        others = await _advisory_held_by_others(worker_db)
        lock_timeout = await s.scalar(text("SHOW lock_timeout"))
        await CheckCoreService.update_check(
            s, plain, CheckUpdate(interval_seconds=777), actor_is_admin=False
        )
        await s.commit()
    await asyncio.wait_for(holder, FINISH)
    async with worker_db() as s:
        interval = await s.scalar(
            select(Check.interval_seconds).where(Check.id == plain)
        )
    # others counts the holder's one lock only: the refused session holds none
    assert (got, others, lock_timeout, interval) == (False, 1, "0", 777), (
        got,
        others,
        lock_timeout,
        interval,
        round(waited, 1),
    )
    assert 2.5 < waited < 5, waited


async def test_granted_guard_resets_lock_timeout(worker_db):
    agent = await _agent(worker_db)
    async with worker_db() as s:
        assert await CheckCRUD.hold_synthetic_guard(s, {agent})
        assert await s.scalar(text("SHOW lock_timeout")) == "0"
        held = await s.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory'"
                " AND classid=1826301560 AND pid = pg_backend_pid()"
            )
        )
        assert held == 1
        await s.rollback()


async def test_guard_taken_in_outer_savepoint_rolled_back_is_released(worker_db):
    """Characterizes PG: a guard taken under a session savepoint that is rolled
    back is released, so a caller that holds it there and goes on unguarded
    would write without it."""
    agent = await _agent(worker_db)
    async with worker_db() as s:
        try:
            async with s.begin_nested():
                assert await CheckCRUD.hold_synthetic_guard(s, {agent})
                raise RuntimeError("entry failed")
        except RuntimeError:
            pass
        held = await s.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory'"
                " AND classid=1826301560 AND pid = pg_backend_pid()"
            )
        )
        await s.rollback()
    assert held == 0, held


async def test_job_session_waits_past_the_request_bound(worker_db):
    """A maintenance job's session (statement_timeout 0) waits for the guard."""
    agent = await _agent(worker_db)
    target = await _check(worker_db, agent, "t")
    ready = asyncio.Event()
    holder = asyncio.create_task(_hold_for(worker_db, {agent}, 4, ready))
    await ready.wait()
    async with worker_db() as s:
        await s.execute(text("SET LOCAL statement_timeout = 0"))
        started = time.monotonic()
        out = await CheckCoreService.bulk_modify(
            s,
            [target],
            CheckUpdate(check_type="synthetic", script_code=SCRIPT),
            actor_is_admin=True,
        )
        waited = time.monotonic() - started
        await s.commit()
    await asyncio.wait_for(holder, FINISH)
    assert out["success_count"] == 1 and waited > 3.5, (out, waited)


SLOW_HOST = "slow.example.test"


def _slow_lookups(monkeypatch, seconds: float) -> None:
    real = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host != SLOW_HOST:
            return real(host, *args, **kwargs)
        time.sleep(seconds)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


async def _job_import(worker_db, agent_id: UUID, synthetic: bool):
    entries = [
        {"name": f"site-{i}", "check_type": "http", "target": f"https://{SLOW_HOST}/"}
        for i in range(3)
    ]
    if synthetic:
        entries.append(
            {
                "name": "b",
                "check_type": "synthetic",
                "target": TARGET,
                "script_code": SCRIPT,
            }
        )
    async with worker_db() as s:
        await s.execute(text("SET LOCAL statement_timeout = 0"))
        out = await CheckTransferCoreService.bulk_import_checks(
            s, agent_id, entries, overwrite=True, actor_is_admin=True
        )
        await s.commit()
        return out


async def _request(worker_db, coro_fn) -> tuple[str, float]:
    started = time.monotonic()
    async with worker_db() as s:
        try:
            await coro_fn(s)
            await s.commit()
            return "done", time.monotonic() - started
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            orig = getattr(e, "orig", None)
            return (
                f"{type(e).__name__}:{type(orig).__name__}:{str(orig)[:120]}",
                time.monotonic() - started,
            )


async def test_edit_during_synthetic_import_is_busy_in_time(worker_db, monkeypatch):
    _slow_lookups(monkeypatch, 2.5)
    agent = await _agent(worker_db)
    plain = await _check(worker_db, agent, "plain")
    importing = asyncio.create_task(_job_import(worker_db, agent, synthetic=True))
    await asyncio.sleep(0.5)

    async def edit(s):
        await CheckCoreService.update_check(
            s, plain, CheckUpdate(interval_seconds=600), actor_is_admin=False
        )

    outcome, waited = await _request(worker_db, edit)
    await asyncio.wait_for(importing, FINISH)
    assert outcome.startswith("CheckBusyException") and waited < 5, (outcome, waited)


async def test_quick_create_during_import_is_busy_not_a_timeout(worker_db, monkeypatch):
    """A request's quick-create from a scan host waits on the agent's names
    hold, which an import holds for its whole run: the request is cut off by
    the 5 s statement timeout (a 500) instead of being answered busy."""
    _slow_lookups(monkeypatch, 2.5)
    agent = await _agent(worker_db)
    importing = asyncio.create_task(_job_import(worker_db, agent, synthetic=False))
    await asyncio.sleep(0.5)

    async def quick(s):
        await JobToCheckCoreService.create_single_check(
            s,
            QuickCheckParams(
                agent_id=agent,
                check_type="http",
                target=TARGET,
                display_name="quick",
                interval=60,
                timeout=10,
                retry_attempts=0,
                tags=[],
                expected_status=200,
            ),
        )

    outcome, waited = await _request(worker_db, quick)
    imported = await asyncio.wait_for(importing, FINISH)
    assert not outcome.startswith("DBAPIError"), (outcome, round(waited, 1), imported)
