"""A delete of many checks never deadlocks against a report in flight.

A report locks the checks its results reference in id order. An agent delete and
the bulk check delete lock the checks they remove FOR UPDATE in id order first,
whatever order they then remove the rows in: the ORM removes a dependent check
before its parent, and `DELETE ... WHERE id IN` takes row locks in scan order.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.core.exceptions import AgentNotFoundException
from app.crud.check_crud import CheckCRUD
from app.db.database import engine
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.util import await_only

pytestmark = pytest.mark.integration

LOW = UUID("00000000-0000-4000-8000-000000000001")
HIGH = UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")


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


# --- An agent delete, once a check depends on another --------------------------


async def _seed_dependency(s: AsyncSession, *, dependent_is_high: bool) -> UUID:
    """Two checks of one agent, one depending on the other: the dependent is the
    higher id (the common case: made after its parent), or the lower."""
    agent = make_agent()
    s.add(agent)
    await s.flush()
    parent_id, child_id = (LOW, HIGH) if dependent_is_high else (HIGH, LOW)
    parent = make_check(agent_id=agent.id, id=parent_id)
    s.add(parent)
    await s.flush()
    child = make_check(agent_id=agent.id, id=child_id, depends_on_check_id=parent_id)
    s.add(child)
    await s.flush()
    return agent.id


async def _race_real_agent_delete(
    worker_db: Any, *, dependent_is_high: bool
) -> tuple[Any, Any]:
    async with worker_db() as s:
        agent_id = await _seed_dependency(s, dependent_is_high=dependent_is_high)
        await s.commit()

    first_done, release = asyncio.Event(), asyncio.Event()
    state = {"n": 0}

    def pause_between(
        conn: Any, cursor: Any, st: str, params: Any, ctx: Any, many: bool
    ) -> None:
        # Pause the agent delete after its first check DELETE has run (the row
        # lock is held), before its second.
        if not st.lstrip().upper().startswith("DELETE FROM CHECKS"):
            return
        state["n"] += 1
        if state["n"] == 2:
            first_done.set()
            await_only(release.wait())

    async def agent_delete() -> None:
        async with worker_db() as op:
            await AgentCoreService.delete_agent(op, agent_id, actor_is_admin=True)
            await op.commit()

    async def report() -> Any:
        async with worker_db() as s:
            out = await CheckResultCoreService.process_agent_report(
                s,
                AgentReportRequest(
                    agent_id=agent_id, checks=[_result(LOW), _result(HIGH)]
                ),
            )
            await s.commit()
            return out

    event.listen(engine.sync_engine, "before_cursor_execute", pause_between)
    try:
        d = asyncio.create_task(agent_delete())
        try:
            await asyncio.wait_for(first_done.wait(), 10)
        except TimeoutError:
            release.set()
            return "delete never issued a second check DELETE", await asyncio.gather(
                d, return_exceptions=True
            )
        r = asyncio.create_task(report())
        await asyncio.sleep(0.7)  # the report locks LOW (or waits) and blocks
        release.set()
        rep, dele = await asyncio.gather(r, d, return_exceptions=True)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", pause_between)
    return rep, dele


def _deadlocked(x: Any) -> bool:
    return isinstance(x, BaseException) and "deadlock" in repr(x).lower()


def _deadlocked(x: Any) -> bool:
    return isinstance(x, BaseException) and "deadlock" in repr(x).lower()


@pytest.mark.parametrize("dependent_is_high", [True, False])
async def test_an_agent_delete_with_a_dependent_check_never_deadlocks_a_report(
    worker_db: Any, dependent_is_high: bool
) -> None:
    """A check of the agent depends on another, its id higher or lower than its
    parent's: the delete removes the dependent first either way."""
    rep, dele = await _race_real_agent_delete(
        worker_db, dependent_is_high=dependent_is_high
    )
    assert not _deadlocked(rep) and not _deadlocked(dele), (
        f"report={rep!r:.300} delete={dele!r:.300}"
    )
    assert not isinstance(dele, BaseException), repr(dele)
    # The delete committed first: the report finds its agent gone.
    assert isinstance(rep, AgentNotFoundException), repr(rep)


# --- The bulk check delete (DELETE ... WHERE id IN, scan order) ----------------


async def _race_bulk_delete(
    worker_db: Any, n: int, *, descending: bool
) -> tuple[Any, Any, str]:
    ids = sorted(UUID(int=(i + 1) << 64 | 0x4000_8000_0000_0000) for i in range(n))
    insert_order = list(reversed(ids)) if descending else ids
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        for cid in insert_order:
            s.add(make_check(agent_id=agent.id, id=cid))
            await s.flush()
        await s.commit()
    async with worker_db() as s:
        plan = "\n".join(
            r[0]
            for r in (
                await s.execute(
                    text(
                        "EXPLAIN DELETE FROM checks WHERE id IN (SELECT unnest(CAST(:ids AS uuid[])))"
                    ),
                    {"ids": ids},
                )
            ).all()
        )

    go = asyncio.Event()
    original = CheckCRUD.key_share_existing

    async def at_go(db: AsyncSession, check_ids: Any) -> Any:
        await go.wait()
        return await original(db, check_ids)

    async def bulk_delete() -> Any:
        async with worker_db() as op:
            # The maintenance worker's session (maintenance_worker.py).
            await op.execute(text("SET LOCAL statement_timeout = 0"))
            await go.wait()
            out = await CheckCoreService.bulk_delete(op, ids, actor_is_admin=True)
            await op.commit()
            return out

    async def report() -> Any:
        async with worker_db() as s:
            out = await CheckResultCoreService.process_agent_report(
                s,
                AgentReportRequest(agent_id=agent.id, checks=[_result(i) for i in ids]),
            )
            await s.commit()
            return out

    mp = pytest.MonkeyPatch()
    mp.setattr(CheckCRUD, "key_share_existing", staticmethod(at_go))
    try:
        r = asyncio.create_task(report())
        d = asyncio.create_task(bulk_delete())
        await asyncio.sleep(0.5)
        go.set()
        rep, dele = await asyncio.gather(r, d, return_exceptions=True)
    finally:
        mp.undo()
    return rep, dele, plan


def _bulk_failed_on_deadlock(dele: Any) -> bool:
    return isinstance(dele, dict) and any(
        "deadlock" in str(e).lower() for e in dele.get("errors", [])
    )


def _bulk_failed_on_deadlock(dele: Any) -> bool:
    return isinstance(dele, dict) and any(
        "deadlock" in str(e).lower() for e in dele.get("errors", [])
    )


@pytest.mark.parametrize("descending", [True, False])
async def test_a_bulk_delete_never_deadlocks_a_report(
    worker_db: Any, descending: bool
) -> None:
    """The checks stored in descending id order (scan order is the reverse of a
    report's lock order) or ascending."""
    rep, dele, plan = await _race_bulk_delete(worker_db, 400, descending=descending)
    assert not _deadlocked(rep) and not _bulk_failed_on_deadlock(dele), (
        f"plan:\n{plan}\nreport={rep!r:.300}\ndelete={dele!r:.400}"
    )
