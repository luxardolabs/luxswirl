"""A delete leaves the checks depending on those it removes (on any agent)
to the clearing of their parent, FOR NO KEY UPDATE: another agent's report on
such a check, which takes it FOR KEY SHARE, is stored while the delete runs.
Two agent deletes whose checks depend on each other's, and an edit naming a
parent on an agent being deleted, finish without a deadlock.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from app.core.datetime_utils import utc_now
from app.crud.check_crud import CheckCRUD
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import text

pytestmark = pytest.mark.integration

REPORT_SECONDS = 2.0
# Past the app engine's statement_timeout (5000 ms, app/db/database.py).
REPORT_SECONDS_REAL = 8.0


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


async def _seed(worker_db) -> tuple[UUID, UUID, UUID, UUID]:
    """Agent A owns parent p; agent B (unrelated) owns child c depending on p."""
    async with worker_db() as s:
        a = make_agent()
        b = make_agent()
        s.add_all([a, b])
        await s.flush()
        p = make_check(agent_id=a.id, id=uuid4(), display_name="parent-on-a")
        s.add(p)
        await s.flush()
        c = make_check(
            agent_id=b.id,
            id=uuid4(),
            display_name="child-on-b",
            depends_on_check_id=p.id,
        )
        s.add(c)
        await s.commit()
        return a.id, b.id, p.id, c.id


async def _warm_chunk(worker_db, b_id: UUID, c_id: UUID) -> None:
    """One result stored first, so the report under test finds today's chunk
    made (a chunk's creation locks the referenced tables, whatever the race)."""
    async with worker_db() as s:
        out = await CheckResultCoreService.process_agent_report(
            s,
            AgentReportRequest(agent_id=b_id, checks=[_result(c_id)]),
            verified_key_hash=None,
        )
        await s.commit()
        assert out.get("results_processed") == 1, out
    await asyncio.sleep(0.01)


async def _report_of_b_while(
    worker_db, deleter, b_id: UUID, c_id: UUID, wait: float = REPORT_SECONDS
) -> str:
    """Run `deleter` in a transaction left open (as a maintenance job's long
    cascade is), then have agent B report a result for its own check c."""
    await _warm_chunk(worker_db, b_id, c_id)
    async with worker_db() as op:
        # The maintenance worker's session lifts the timeouts.
        await op.execute(text("SET LOCAL statement_timeout = 0"))
        await deleter(op)
        try:
            async with worker_db() as s:
                try:
                    probe = asyncio.create_task(_blocked_query(worker_db))
                    out = await asyncio.wait_for(
                        CheckResultCoreService.process_agent_report(
                            s,
                            AgentReportRequest(agent_id=b_id, checks=[_result(c_id)]),
                            verified_key_hash=None,
                        ),
                        wait,
                    )
                    probe.cancel()
                    await s.commit()
                    return f"stored:{out.get('results_processed')}"
                except TimeoutError:
                    await s.rollback()
                    blocked = await probe
                    return (
                        "report blocked by the delete of another agent's check: "
                        + blocked
                    )
                except Exception as e:  # the outcome is what is asserted
                    await s.rollback()
                    return f"error:{type(e).__name__}"
        finally:
            await op.rollback()


async def _agent_delete(a_id: UUID):
    async def run(op):
        await AgentCoreService.delete_agent(op, a_id, actor_is_admin=True)

    return run


async def _bulk_delete(p_id: UUID):
    async def run(op):
        out = await CheckCoreService.bulk_delete(op, [p_id], actor_is_admin=False)
        assert out["success_count"] == 1, out

    return run


async def _blocked_query(worker_db) -> str:
    await asyncio.sleep(1.0)
    async with worker_db() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT a.query, l.locktype, l.mode, l.relation::regclass::text "
                    "FROM pg_stat_activity a JOIN pg_locks l ON l.pid = a.pid "
                    "WHERE NOT l.granted"
                )
            )
        ).all()
        return repr([(r[0][:160], r[1], r[2], r[3]) for r in rows])


# --- Another agent's reports during a delete of agent A ---------------------


async def test_agent_delete_does_not_block_another_agents_reports(worker_db):
    a_id, b_id, _, c_id = await _seed(worker_db)
    outcome = await _report_of_b_while(worker_db, await _agent_delete(a_id), b_id, c_id)
    assert outcome == "stored:1", outcome


async def test_bulk_delete_of_a_parent_does_not_block_its_childs_agent_reports(
    worker_db,
):
    _, b_id, p_id, c_id = await _seed(worker_db)
    outcome = await _report_of_b_while(worker_db, await _bulk_delete(p_id), b_id, c_id)
    assert outcome == "stored:1", outcome


async def test_an_unrelated_check_of_another_agent_reports_during_an_agent_delete(
    worker_db,
):
    """A check of B that depends on nothing reports through A's delete."""
    a_id, b_id, _, _ = await _seed(worker_db)
    async with worker_db() as s:
        free = make_check(agent_id=b_id, id=uuid4(), display_name="free-on-b")
        s.add(free)
        await s.commit()
        free_id = free.id
    outcome = await _report_of_b_while(
        worker_db, await _agent_delete(a_id), b_id, free_id
    )
    assert outcome == "stored:1", outcome


# --- Two agent deletes whose checks depend on each other's -------------------


async def test_two_agent_deletes_with_crossed_dependencies_do_not_deadlock(worker_db):
    async with worker_db() as s:
        a, b = make_agent(), make_agent()
        s.add_all([a, b])
        await s.flush()
        ids = sorted(uuid4() for _ in range(4))
        # interleave so each pass meets the other's rows on both sides
        a_p = make_check(agent_id=a.id, id=ids[0], display_name="a_p")
        b_p = make_check(agent_id=b.id, id=ids[1], display_name="b_p")
        s.add_all([a_p, b_p])
        await s.flush()
        s.add(
            make_check(
                agent_id=a.id, id=ids[3], display_name="a1", depends_on_check_id=ids[1]
            )
        )
        s.add(
            make_check(
                agent_id=b.id, id=ids[2], display_name="b1", depends_on_check_id=ids[0]
            )
        )
        await s.commit()
        a_id, b_id = a.id, b.id

    async def delete(agent_id):
        async with worker_db() as s:
            try:
                await AgentCoreService.delete_agent(s, agent_id, actor_is_admin=True)
                await asyncio.sleep(0.5)
                await s.commit()
                return "deleted"
            except Exception as e:  # the outcome is what is asserted
                await s.rollback()
                return f"error:{type(e).__name__}:{e!s:.200}"

    out = await asyncio.wait_for(asyncio.gather(delete(a_id), delete(b_id)), 30)
    assert out == ["deleted", "deleted"], out


async def test_another_agents_report_is_stored_within_the_statement_timeout_during_an_agent_delete(
    worker_db,
):
    """On the real engine (statement_timeout 5 s), B's report is stored while
    A's delete runs."""
    a_id, b_id, _, c_id = await _seed(worker_db)
    outcome = await _report_of_b_while(
        worker_db, await _agent_delete(a_id), b_id, c_id, wait=REPORT_SECONDS_REAL
    )
    assert outcome == "stored:1", outcome


# --- An edit naming a parent on another agent, while that agent is deleted ---


@pytest.mark.parametrize("parent_first", [True, False])
async def test_cross_agent_parent_edit_and_agent_delete_do_not_deadlock(
    worker_db, monkeypatch, parent_first
):
    from app.schemas.check_schema import CheckUpdate

    low, high = sorted([uuid4(), uuid4()])
    p_id, c_id = (low, high) if parent_first else (high, low)
    async with worker_db() as s:
        a, b = make_agent(), make_agent()
        s.add_all([a, b])
        await s.flush()
        s.add(make_check(agent_id=a.id, id=p_id, display_name="p"))
        s.add(make_check(agent_id=b.id, id=c_id, display_name="c"))
        await s.commit()
        a_id = a.id
    real = CheckCRUD.save_check

    async def slow_save(db, check):
        await asyncio.sleep(1.5)
        return await real(db, check)

    monkeypatch.setattr(CheckCRUD, "save_check", staticmethod(slow_save))

    async def edit():
        async with worker_db() as s:
            try:
                await CheckCoreService.update_check(
                    s, c_id, CheckUpdate(depends_on_check_id=p_id), actor_is_admin=False
                )
                await s.commit()
                return "edited"
            except Exception as e:  # the outcome is what is asserted
                await s.rollback()
                return f"error:{type(e).__name__}:{e!s:.200}"

    async def delete():
        async with worker_db() as s:
            try:
                await AgentCoreService.delete_agent(s, a_id, actor_is_admin=False)
                await s.commit()
                return "deleted"
            except Exception as e:  # the outcome is what is asserted
                await s.rollback()
                return f"error:{type(e).__name__}:{e!s:.200}"

    e = asyncio.create_task(edit())
    await asyncio.sleep(0.5)
    d = asyncio.create_task(delete())
    out = await asyncio.wait_for(asyncio.gather(e, d), 30)
    assert "deadlock" not in str(out).lower(), out
    assert out[1] == "deleted", out
    async with worker_db() as s:
        child = await CheckCRUD.get_by_id(s, c_id)
        assert child is not None and child.depends_on_check_id is None, out
