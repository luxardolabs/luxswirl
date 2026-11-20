"""A check made with a parent (a create, a clone) while the parent is
deleted, by a check delete, an agent delete or a bulk delete, and an edit
naming that parent meanwhile: none deadlocks."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.schemas.check_schema import CheckCreate, CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import text

pytestmark = pytest.mark.integration

# The parent sorts after every random uuid4 child.
PARENT_ID = UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")


def _result(check_id: UUID) -> CheckResultCreate:
    return CheckResultCreate(
        timestamp=utc_now(),
        success=True,
        latency_ms=5.0,
        check_id=str(check_id),
        display_name="probe",
        check_type="ping",
        target="192.0.2.10",
    )


async def _seed(worker_db) -> tuple[UUID, UUID]:
    async with worker_db() as s:
        a = make_agent()
        b = make_agent()
        s.add_all([a, b])
        await s.flush()
        b_id = b.id
        s.add(
            make_check(
                agent_id=a.id, id=PARENT_ID, display_name="x", target="192.0.2.10"
            )
        )
        await s.commit()
        a_id = a.id
    # A result stored first, so the open report below makes no chunk.
    async with worker_db() as s:
        out = await CheckResultCoreService.process_agent_report(
            s,
            AgentReportRequest(agent_id=a_id, checks=[_result(PARENT_ID)]),
            verified_key_hash=None,
        )
        await s.commit()
        assert out.get("results_processed") == 1, out
    await asyncio.sleep(0.01)
    return a_id, b_id


async def _outcome(worker_db, work) -> str:
    async with worker_db() as s:
        try:
            out = await work(s)
            await s.commit()
            return out
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return f"error:{type(e).__name__}:{e!s:.160}"


async def _run(
    worker_db, *, child_before_delete: bool, deleter: str = "check"
) -> list[str]:
    a_id, b_id = await _seed(worker_db)

    # R: agent A's report on X, held open (it holds X FOR KEY SHARE).
    report = worker_db()
    r = await report.__aenter__()
    out = await CheckResultCoreService.process_agent_report(
        r,
        AgentReportRequest(agent_id=a_id, checks=[_result(PARENT_ID)]),
        verified_key_hash=None,
    )
    assert out.get("results_processed") == 1, out

    # T1: an editor makes a check N depending on X (FOR KEY SHARE on X).
    maker = worker_db()
    t1 = await maker.__aenter__()
    child = await CheckCoreService.create_check(
        t1,
        b_id,
        CheckCreate(
            display_name="n",
            check_type="ping",
            target="192.0.2.11",
            depends_on_check_id=PARENT_ID,
        ),
        actor_is_admin=False,
    )
    child_id = child.id
    assert child_id < PARENT_ID
    if child_before_delete:
        await t1.commit()
        await maker.__aexit__(None, None, None)

    async def delete(s):
        await s.execute(text("SET LOCAL statement_timeout = 0"))
        if deleter == "agent":
            await AgentCoreService.delete_agent(s, a_id, actor_is_admin=False)
        elif deleter == "bulk":
            res = await CheckCoreService.bulk_delete(
                s, [PARENT_ID], actor_is_admin=False
            )
            if res["success_count"] != 1:
                return f"bulk:{res!r:.300}"
        else:
            await CheckCoreService.delete_check(s, PARENT_ID, actor_is_admin=False)
        return "deleted"

    # D: an editor deletes X while R (and T1) are in flight.
    d = asyncio.create_task(_outcome(worker_db, delete))
    await asyncio.sleep(1.0)
    if not child_before_delete:
        # N committed while D is in flight.
        await t1.commit()
        await maker.__aexit__(None, None, None)
        await asyncio.sleep(0.3)

    async def edit(s):
        await CheckCoreService.update_check(
            s,
            child_id,
            CheckUpdate(depends_on_check_id=PARENT_ID),
            actor_is_admin=False,
        )
        return "edited"

    # T2: another editor sets N's parent to X.
    e = asyncio.create_task(_outcome(worker_db, edit))
    await asyncio.sleep(1.0)
    # R ends; D goes on.
    await r.commit()
    await report.__aexit__(None, None, None)
    outcomes = list(await asyncio.wait_for(asyncio.gather(d, e), 30))

    async with worker_db() as s:
        left = (
            await s.execute(
                text("SELECT id, depends_on_check_id FROM checks WHERE id IN (:a, :b)"),
                {"a": PARENT_ID, "b": child_id},
            )
        ).all()
    return [*outcomes, repr(left)]


DELETERS = ["check", "agent", "bulk"]


@pytest.mark.parametrize("deleter", DELETERS)
async def test_a_child_made_before_its_parents_delete_waits_without_deadlock(
    worker_db, deleter
):
    """N exists before the delete starts."""
    out = await _run(worker_db, child_before_delete=True, deleter=deleter)
    assert "deadlock" not in str(out).lower(), out
    assert out[0] == "deleted", out


@pytest.mark.parametrize("deleter", DELETERS)
async def test_a_child_made_while_its_parent_is_deleted_does_not_deadlock(
    worker_db, deleter
):
    """N is made while the delete is in flight, and an edit then names X as
    N's parent."""
    out = await _run(worker_db, child_before_delete=False, deleter=deleter)
    assert "deadlock" not in str(out).lower(), out
    assert out[0] == "deleted", out
