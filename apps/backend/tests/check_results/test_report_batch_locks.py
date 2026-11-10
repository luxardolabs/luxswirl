"""How a report's locks meet a delete running beside it.

The report locks the checks its results reference, then the agent, FOR KEY SHARE,
in the order an agent delete removes them (its checks by id, then the agent): the
two never deadlock. An agent deleted while its report is in flight is not found,
never a foreign-key 500, and a check edit that changes no key is never blocked.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.core.datetime_utils import utc_now
from app.core.exceptions import AgentNotFoundException
from app.crud.check_crud import CheckCRUD
from app.db.database import engine
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import delete, event, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


def _result(check_id: UUID, ts: Any = None) -> CheckResultCreate:
    return CheckResultCreate(
        timestamp=ts or utc_now(),
        success=True,
        latency_ms=5.0,
        check_id=str(check_id),
        display_name="probe",
        check_type="ping",
        target="127.0.0.1",
    )


def _delete_agent_mid_flight(monkeypatch: pytest.MonkeyPatch, agent_id: UUID) -> None:
    read = AgentAssignmentCoreService.get_reportable_checks

    async def read_then_delete(session: AsyncSession, *a: Any, **k: Any) -> Any:
        checks = await read(session, *a, **k)
        await session.execute(delete(Agent).where(Agent.id == agent_id))
        return checks

    monkeypatch.setattr(
        AgentAssignmentCoreService, "get_reportable_checks", read_then_delete
    )


# --- The reporting agent deleted mid-flight -------------------------------------


async def test_an_agent_deleted_mid_flight_is_not_found_not_a_foreign_key_500(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent X reports a replicate check owned by agent Y; X is deleted after the
    report read its checks. The check survives (it is Y's)."""
    reporter = make_agent(agent_name=f"rep-{uuid4().hex[:6]}")
    owner = make_agent(agent_name=f"own-{uuid4().hex[:6]}")
    db.add_all([reporter, owner])
    await db.flush()
    replicated = make_check(
        agent_id=owner.id,
        assignment_mode="replicate",
        agent_selector={"agent_ids": [reporter.agent_name]},
    )
    db.add(replicated)
    await db.flush()
    _delete_agent_mid_flight(monkeypatch, reporter.id)

    with pytest.raises(AgentNotFoundException):
        await CheckResultCoreService.process_agent_report(
            db,
            AgentReportRequest(agent_id=reporter.id, checks=[_result(replicated.id)]),
        )


async def test_an_agent_deleted_mid_flight_with_only_its_own_checks_is_not_found(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    reporter = make_agent()
    db.add(reporter)
    await db.flush()
    own = make_check(agent_id=reporter.id)
    db.add(own)
    await db.flush()
    own_id = own.id
    _delete_agent_mid_flight(monkeypatch, reporter.id)

    with pytest.raises(AgentNotFoundException):
        await CheckResultCoreService.process_agent_report(
            db, AgentReportRequest(agent_id=reporter.id, checks=[_result(own_id)])
        )


# --- Lock order against an agent delete ------------------------------------------


async def test_an_agent_delete_removes_its_checks_in_id_order(
    db: AsyncSession,
) -> None:
    """The order the report's lock follows: what AgentCoreService.delete_agent
    sends."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    checks = [make_check(agent_id=agent.id) for _ in range(6)]
    db.add_all(checks)
    await db.flush()
    seen: list[tuple[str, Any, bool]] = []

    def capture(
        conn: Any,
        cursor: Any,
        statement: str,
        params: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("DELETE"):
            seen.append((statement.split(" WHERE")[0], params, executemany))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await AgentCoreService.delete_agent(db, agent.id, actor_is_admin=True)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    check_deletes = [s for s in seen if s[0].startswith("DELETE FROM checks")]
    assert check_deletes, seen
    _stmt, params, many = check_deletes[0]
    ids = [p[0] for p in params] if many else [params[0]]
    assert many and len(ids) == 6
    assert list(ids) == sorted(c.id for c in checks)


async def _seed_checks(worker_db: Any, n: int) -> tuple[UUID, list[UUID]]:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        ids: list[UUID] = []
        for _ in range(n):
            c = make_check(agent_id=agent.id)
            s.add(c)
            await s.flush()  # one by one: physical order = this order
            ids.append(c.id)
        await s.commit()
    return agent.id, ids


async def _race_agent_style_delete(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any, str]:
    agent_id, ids = await _seed_checks(worker_db, 60)
    by_id = sorted(ids)  # the ORM's delete order (probe above)
    first = by_id[0]
    first_deleted = asyncio.Event()
    async with worker_db() as s:
        plan = "\n".join(
            r[0]
            for r in (
                await s.execute(
                    text(
                        "EXPLAIN SELECT id FROM checks WHERE id = ANY(:ids) FOR KEY SHARE"
                    ),
                    {"ids": ids},
                )
            ).all()
        )

    original = CheckCRUD.key_share_existing

    async def after_agent_delete_started(db: AsyncSession, check_ids: Any) -> Any:
        await first_deleted.wait()
        return await original(db, check_ids)

    monkeypatch.setattr(
        CheckCRUD, "key_share_existing", staticmethod(after_agent_delete_started)
    )

    async def agent_delete() -> None:
        # The agent delete's flush: DELETE FROM checks WHERE id = $1, row by row in
        # id order, each followed by its results' cascade; then the agent row.
        async with worker_db() as op:
            await op.execute(delete(Check).where(Check.id == first))
            first_deleted.set()
            await asyncio.sleep(0.3)  # the first check's results cascade
            for cid in by_id[1:]:
                await op.execute(delete(Check).where(Check.id == cid))
            await op.execute(delete(Agent).where(Agent.id == agent_id))
            await op.commit()

    async def report() -> Any:
        async with worker_db() as s:
            out = await CheckResultCoreService.process_agent_report(
                s,
                AgentReportRequest(agent_id=agent_id, checks=[_result(i) for i in ids]),
            )
            await s.commit()
            return out

    rep, dele = await asyncio.gather(report(), agent_delete(), return_exceptions=True)
    return rep, dele, plan


async def test_a_report_never_deadlocks_against_an_agent_delete(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rep, dele, plan = await _race_agent_style_delete(worker_db, monkeypatch)
    assert not isinstance(dele, BaseException), f"plan:\n{plan}\ndelete={dele!r:.400}"
    # The delete committed first: the report finds its agent gone, or (had it
    # locked first) stored its results. Never a deadlock.
    assert isinstance(rep, AgentNotFoundException) or (
        isinstance(rep, dict) and rep["results_processed"] + rep["results_failed"] == 60
    ), f"plan:\n{plan}\nreport={rep!r:.400}"


# --- What the held lock leaves free ----------------------------------------------


async def _hold_in_alert_eval(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[UUID, UUID, asyncio.Event, asyncio.Task[Any]]:
    """A report of a new result for `fresh` and a duplicate for `dup`, paused in
    alert evaluation (after the insert, locks held)."""
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        fresh, dup = make_check(agent_id=agent.id), make_check(agent_id=agent.id)
        s.add_all([fresh, dup])
        await s.commit()
    ts = utc_now()
    async with worker_db() as s:
        await CheckResultCoreService.process_agent_report(
            s, AgentReportRequest(agent_id=agent.id, checks=[_result(dup.id, ts)])
        )
        await s.commit()

    paused, release = asyncio.Event(), asyncio.Event()

    async def slow_eval(db: AsyncSession, result: Any) -> None:
        paused.set()
        await release.wait()

    monkeypatch.setattr(
        AlertEvaluationCoreService, "evaluate_check_result", staticmethod(slow_eval)
    )

    async def report() -> Any:
        async with worker_db() as s:
            out = await CheckResultCoreService.process_agent_report(
                s,
                AgentReportRequest(
                    agent_id=agent.id, checks=[_result(fresh.id), _result(dup.id, ts)]
                ),
            )
            await s.commit()
            return out

    task = asyncio.create_task(report())
    await asyncio.wait_for(paused.wait(), 5)
    return fresh.id, dup.id, release, task


async def _runs_within(worker_db: Any, stmt: Any, seconds: float) -> bool:
    async def go() -> None:
        async with worker_db() as op:
            await op.execute(text("SET LOCAL lock_timeout = '3s'"))
            await op.execute(stmt)
            await op.commit()

    try:
        await asyncio.wait_for(go(), seconds)
        return True
    except TimeoutError, DBAPIError:
        return False


async def test_a_non_key_check_edit_is_not_blocked_by_the_report(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh, dup, release, task = await _hold_in_alert_eval(worker_db, monkeypatch)
    try:
        edited = await _runs_within(
            worker_db,
            update(Check)
            .where(Check.id.in_([fresh, dup]))
            .values(interval_seconds=120, display_name="renamed"),
            2.0,
        )
    finally:
        release.set()
        await task
    assert edited
