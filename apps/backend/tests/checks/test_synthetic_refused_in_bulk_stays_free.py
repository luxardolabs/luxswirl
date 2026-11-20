"""A check an editor's bulk write refuses (a synthetic one) is not held by
that write: its agent's reports, and an admin's alert change on it, neither
wait for nor are refused because of a job that does not touch it."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from app.core.datetime_utils import utc_now
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.schemas.check_schema import CheckUpdate
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import text

pytestmark = pytest.mark.integration

REPORT_SECONDS = 2.0


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
    """Agent A owns plain x; agent B owns synthetic s (nothing links them)."""
    async with worker_db() as s:
        a, b = make_agent(), make_agent()
        s.add_all([a, b])
        await s.flush()
        x = make_check(agent_id=a.id, id=uuid4(), display_name="plain-on-a")
        syn = make_check(
            agent_id=b.id,
            id=uuid4(),
            display_name="synthetic-on-b",
            check_type="synthetic",
            target="https://example.com",
            script_code="async def run(page):\n    await page.goto('https://example.com')\n",
        )
        s.add_all([x, syn])
        await s.commit()
        return a.id, b.id, x.id, syn.id


async def _report(worker_db, agent_id: UUID, check_id: UUID, wait: float) -> str:
    async with worker_db() as s:
        try:
            out = await asyncio.wait_for(
                CheckResultCoreService.process_agent_report(
                    s,
                    AgentReportRequest(agent_id=agent_id, checks=[_result(check_id)]),
                    verified_key_hash=None,
                ),
                wait,
            )
            await s.commit()
            return f"stored:{out.get('results_processed')}"
        except TimeoutError:
            await s.rollback()
            return "report blocked"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return f"error:{type(e).__name__}"


async def _admin_alert_judgement(worker_db, check_id: UUID) -> str:
    async with worker_db() as s:
        try:
            await AlertCoreService.require_checks(s, [check_id], actor_is_admin=True)
            await s.rollback()
            return "judged"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return f"error:{type(e).__name__}"


async def _while_editor_job_open(worker_db, job, probe) -> tuple[dict, str]:
    """Run the editor's job in a transaction left open (a maintenance job's
    long cascade or long loop), then run `probe` from another session."""
    async with worker_db() as op:
        await op.execute(text("SET LOCAL statement_timeout = 0"))
        try:
            out = await job(op)
            seen = await probe()
            return out, seen
        finally:
            await op.rollback()


async def _warm(worker_db, b_id: UUID, syn_id: UUID) -> None:
    """Today's chunk made first (its creation locks the referenced tables)."""
    assert await _report(worker_db, b_id, syn_id, 30) == "stored:1"


# --- An editor's bulk delete naming a synthetic check it is refused ---------


async def test_an_editors_refused_synthetic_in_a_bulk_delete_leaves_its_reports_free(
    worker_db,
):
    _, b_id, x_id, syn_id = await _seed(worker_db)
    await _warm(worker_db, b_id, syn_id)

    async def job(op):
        return await CheckCoreService.bulk_delete(
            op, [x_id, syn_id], actor_is_admin=False
        )

    out, seen = await _while_editor_job_open(
        worker_db, job, lambda: _report(worker_db, b_id, syn_id, REPORT_SECONDS)
    )
    assert out["success_count"] == 1 and out["failure_count"] == 1, out
    assert seen == "stored:1", seen


async def test_a_bulk_delete_not_naming_the_synthetic_leaves_its_reports_free(
    worker_db,
):
    _, b_id, x_id, syn_id = await _seed(worker_db)
    await _warm(worker_db, b_id, syn_id)

    async def job(op):
        return await CheckCoreService.bulk_delete(op, [x_id], actor_is_admin=False)

    out, seen = await _while_editor_job_open(
        worker_db, job, lambda: _report(worker_db, b_id, syn_id, REPORT_SECONDS)
    )
    assert out["success_count"] == 1, out
    assert seen == "stored:1", seen


async def test_an_editors_refused_synthetic_in_a_bulk_delete_leaves_an_admins_alert_change_free(
    worker_db,
):
    _, _, x_id, syn_id = await _seed(worker_db)

    async def job(op):
        return await CheckCoreService.bulk_delete(
            op, [x_id, syn_id], actor_is_admin=False
        )

    out, seen = await _while_editor_job_open(
        worker_db, job, lambda: _admin_alert_judgement(worker_db, syn_id)
    )
    assert out["failure_count"] == 1, out
    assert seen == "judged", seen


# --- An editor's bulk modify naming a synthetic check it is refused ---------


async def test_an_editors_refused_synthetic_in_a_bulk_modify_leaves_an_admins_alert_change_free(
    worker_db,
):
    _, _, x_id, syn_id = await _seed(worker_db)

    async def job(op):
        return await CheckCoreService.bulk_modify(
            op,
            [x_id, syn_id],
            CheckUpdate(interval_seconds=120),
            actor_is_admin=False,
        )

    out, seen = await _while_editor_job_open(
        worker_db, job, lambda: _admin_alert_judgement(worker_db, syn_id)
    )
    assert out["success_count"] == 1 and out["failure_count"] == 1, out
    assert seen == "judged", seen


async def test_a_bulk_modify_not_naming_the_synthetic_leaves_an_admins_alert_change_free(
    worker_db,
):
    _, _, x_id, syn_id = await _seed(worker_db)

    async def job(op):
        return await CheckCoreService.bulk_modify(
            op, [x_id], CheckUpdate(interval_seconds=120), actor_is_admin=False
        )

    out, seen = await _while_editor_job_open(
        worker_db, job, lambda: _admin_alert_judgement(worker_db, syn_id)
    )
    assert out["success_count"] == 1, out
    assert seen == "judged", seen
