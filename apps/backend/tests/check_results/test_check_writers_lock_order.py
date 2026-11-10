"""Every writer of many checks locks them in id order, so none deadlocks another.

1. The stale-agent cleanup (`cleanup_stale_agents`, a scheduled job) deletes
   agents with one `DELETE FROM agents`; the database cascade removes their checks
   agent by agent. It locks those checks in id order first, as a live agent's
   report on replicate checks owned by two stale agents locks them.
2. The bulk enable/disable updates in scan order; it locks the rows in id order
   first (FOR NO KEY UPDATE), as the bulk check delete locks them.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.crud.agent_crud import AgentCRUD
from app.crud.check_crud import CheckCRUD
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.services.core import cleanup_core_service
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from fixtures.factories import make_agent, make_check, make_check_result
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

LOW = UUID("00000000-0000-4000-8000-000000000001")
HIGH = UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
RESULT_ID = UUID("11111111-1111-4111-8111-111111111111")


def _result(check_id: UUID) -> CheckResultCreate:
    return CheckResultCreate(
        timestamp=utc_now(),
        success=True,
        latency_ms=5.0,
        check_id=str(check_id),
        display_name="probe",
        check_type="ping",
        target="127.0.0.1",
    )


def _deadlocked(x: Any) -> bool:
    return isinstance(x, BaseException) and "deadlock" in repr(x).lower()


# --- The stale-agent cleanup ----------------------------------------------------


async def _seed_stale(s: AsyncSession) -> tuple[UUID, UUID]:
    """A live agent B, and two stale agents A1 then A2 (stored in that order),
    A1 owning the HIGH replicate check and A2 the LOW one, both run by B. A
    result of B's on HIGH, for the blocker to hold."""
    live = make_agent()
    s.add(live)
    await s.flush()
    old = utc_now() - timedelta(days=60)
    a1 = make_agent(last_seen=old, first_seen=old)
    s.add(a1)
    await s.flush()
    a2 = make_agent(last_seen=old, first_seen=old)
    s.add(a2)
    await s.flush()
    selector = {"agent_ids": [live.agent_name]}
    s.add(
        make_check(
            agent_id=a1.id,
            id=HIGH,
            assignment_mode="replicate",
            agent_selector=selector,
        )
    )
    await s.flush()
    s.add(
        make_check(
            agent_id=a2.id, id=LOW, assignment_mode="replicate", agent_selector=selector
        )
    )
    await s.flush()
    s.add(
        make_check_result(
            id=RESULT_ID,
            check_id=HIGH,
            agent_id=a1.id,
            timestamp=utc_now() - timedelta(minutes=5),
        )
    )
    await s.flush()
    return live.id, a1.id


async def _race_stale_cleanup(worker_db: Any) -> tuple[Any, Any]:
    async with worker_db() as s:
        live_id, _ = await _seed_stale(s)
        await s.commit()

    async def cleanup() -> Any:
        return await cleanup_core_service.cleanup_stale_agents(inactive_days=30)

    async def report() -> Any:
        async with worker_db() as s:
            out = await CheckResultCoreService.process_agent_report(
                s,
                AgentReportRequest(
                    agent_id=live_id, checks=[_result(LOW), _result(HIGH)]
                ),
            )
            await s.commit()
            return out

    # The blocker holds a result of A1's (on HIGH), so the cleanup is held inside
    # A1's cascade (per agent row: agent_metrics, checks, check_results; A1 then
    # A2, so HIGH is removed before LOW) while the report starts.
    async with worker_db() as blocker:
        await blocker.execute(
            text("SELECT id FROM check_results WHERE id = :id FOR UPDATE"),
            {"id": RESULT_ID},
        )
        c = asyncio.create_task(cleanup())
        await asyncio.sleep(0.7)
        r = asyncio.create_task(report())  # locks LOW, waits on HIGH
        await asyncio.sleep(0.7)
        await blocker.commit()
    rep, cln = await asyncio.wait_for(asyncio.gather(r, c, return_exceptions=True), 30)
    return rep, cln


async def test_stale_agent_cleanup_never_deadlocks_a_report(worker_db: Any) -> None:
    rep, cln = await _race_stale_cleanup(worker_db)
    assert not _deadlocked(rep) and not _deadlocked(cln), (
        f"report={rep!r:.400}\ncleanup={cln!r:.400}"
    )
    assert isinstance(cln, dict) and cln["deleted"] >= 2, repr(cln)
    # The report waited for the cleanup, then found both checks gone.
    assert isinstance(rep, dict) and rep["results_processed"] == 0, repr(rep)


# --- Bulk enable vs bulk delete --------------------------------------------------


def _ids(n: int) -> list[UUID]:
    return sorted(UUID(int=(i + 1) << 64 | 0x4000_8000_0000_0000) for i in range(n))


async def _race_enable_delete(worker_db: Any, n: int) -> tuple[Any, Any, str]:
    ids = _ids(n)
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        for cid in reversed(ids):  # stored in descending id order
            s.add(make_check(agent_id=agent.id, id=cid, enabled=True))
            await s.flush()
        await s.commit()
    async with worker_db() as s:
        plan = "\n".join(
            r[0]
            for r in (
                await s.execute(
                    text(
                        "EXPLAIN UPDATE checks SET enabled = false "
                        "WHERE id IN (SELECT unnest(CAST(:ids AS uuid[])))"
                    ),
                    {"ids": ids},
                )
            ).all()
        )
    middle = ids[n // 2]

    async def enable() -> Any:
        async with worker_db() as op:
            out = await CheckCoreService.bulk_set_enabled(
                op, ids, enabled=False, actor_is_admin=True
            )
            await op.commit()
            return out

    async def bulk_delete() -> Any:
        async with worker_db() as op:
            await op.execute(text("SET LOCAL statement_timeout = 0"))
            out = await CheckCoreService.bulk_delete(op, ids, actor_is_admin=True)
            await op.commit()
            return out

    async with worker_db() as blocker:
        await blocker.execute(
            text("SELECT id FROM checks WHERE id = :id FOR SHARE"), {"id": middle}
        )
        e = asyncio.create_task(enable())  # scan order: high ids, waits mid
        await asyncio.sleep(0.7)
        d = asyncio.create_task(bulk_delete())  # id order: low ids, waits mid
        await asyncio.sleep(0.7)
        await blocker.commit()
    en, de = await asyncio.wait_for(asyncio.gather(e, d, return_exceptions=True), 30)
    return en, de, plan


def _bulk_deadlocked(x: Any) -> bool:
    if _deadlocked(x):
        return True
    return isinstance(x, dict) and any(
        "deadlock" in str(err).lower() for err in x.get("errors", [])
    )


async def test_bulk_enable_never_deadlocks_a_bulk_delete(worker_db: Any) -> None:
    """The checks stored in descending id order: the enable's UPDATE scans them
    the other way round from the delete's id-ordered lock."""
    en, de, plan = await _race_enable_delete(worker_db, 60)
    assert not _bulk_deadlocked(en) and not _bulk_deadlocked(de), (
        f"plan:\n{plan}\nenable={en!r:.400}\ndelete={de!r:.400}"
    )
    assert isinstance(de, dict) and de["success_count"] == 60, repr(de)


async def test_lock_for_delete_edges_and_agent_with_no_checks(worker_db: Any) -> None:
    """An empty selection and no selection lock nothing and do not fail; an agent
    with no checks deletes."""
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        await s.commit()
        agent_id = agent.id
    async with worker_db() as s:
        await CheckCRUD.lock_for_delete(s, check_ids=[])
        await CheckCRUD.lock_for_delete(s)
        await AgentCoreService.delete_agent(s, agent_id, actor_is_admin=True)
        await s.commit()
    async with worker_db() as s:
        assert await AgentCRUD.key_share_exists(s, agent_id) is False
