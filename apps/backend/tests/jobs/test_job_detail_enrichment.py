"""Scan enrichment is a display transform, and must not reach the database.

The job detail panel shows a network_scan result with host categories, summary
stats and recommendations added. That enrichment used to be applied in the view
by assigning the enriched payload back onto the ORM row — which meant the
transform was one flush away from being written into the jobs table, and the
next read would enrich already-enriched data.

It now happens on the DTO in the core service. These pin both halves: the
caller gets the enriched result, and the stored row still holds exactly what
the agent reported.
"""

from __future__ import annotations

from uuid import uuid4

from app.crud.job_crud import JobCRUD
from app.models.enum_model import JobType
from app.models.job_model import Job
from app.services.core.job_core_service import JobCoreService
from fixtures.factories import make_agent
from sqlalchemy.ext.asyncio import AsyncSession

RAW_RESULT = {
    "hosts": [
        {"ip": "192.168.1.10", "ports": [22, 80, 443]},
        {"ip": "192.168.1.11", "ports": [3306]},
    ],
    "subnet": "192.168.1.0/24",
}


async def _scan_job(db: AsyncSession, *, result: dict | None = None) -> Job:
    job = Job(
        job_type=JobType.NETWORK_SCAN,
        agent_id=None,
        params={"subnet": "192.168.1.0/24"},
        status="completed",
        result=result,
    )
    db.add(job)
    await db.flush()
    return job


class TestEnrichmentStaysOnTheDto:
    async def test_detail_result_is_enriched(self, db: AsyncSession):
        job = await _scan_job(db, result=RAW_RESULT)

        detail = await JobCoreService.get_job_detail(db, job.id)

        assert detail is not None
        assert detail.result is not None
        # The raw payload is still there, with analysis added on top.
        assert detail.result["subnet"] == "192.168.1.0/24"
        assert set(detail.result) > set(RAW_RESULT)

    async def test_the_stored_row_is_untouched(self, db: AsyncSession):
        job = await _scan_job(db, result=RAW_RESULT)

        await JobCoreService.get_job_detail(db, job.id)
        await db.flush()

        stored = await JobCRUD.get_by_id(db, job.id)
        assert stored is not None
        assert stored.result == RAW_RESULT

    async def test_reading_twice_does_not_compound(self, db: AsyncSession):
        """Enriching an already-enriched result was the failure mode of writing
        the transform back to the row."""
        job = await _scan_job(db, result=RAW_RESULT)

        first = await JobCoreService.get_job_detail(db, job.id)
        second = await JobCoreService.get_job_detail(db, job.id)

        assert first is not None and second is not None
        assert first.result == second.result

    async def test_a_scan_with_no_result_is_left_alone(self, db: AsyncSession):
        job = await _scan_job(db, result=None)

        detail = await JobCoreService.get_job_detail(db, job.id)

        assert detail is not None
        assert detail.result is None

    async def test_a_non_scan_job_is_not_enriched(self, db: AsyncSession):
        job = Job(
            job_type=JobType.NETWORK_DISCOVER,
            agent_id=None,
            params={},
            status="completed",
            result=RAW_RESULT,
        )
        db.add(job)
        await db.flush()

        detail = await JobCoreService.get_job_detail(db, job.id)

        assert detail is not None
        assert detail.result == RAW_RESULT

    async def test_missing_job_is_none(self, db: AsyncSession):
        assert await JobCoreService.get_job_detail(db, uuid4()) is None


class TestRunnerToken:
    async def test_server_job_reports_the_server_token(self, db: AsyncSession):
        job = await _scan_job(db, result=None)

        detail = await JobCoreService.get_job_detail(db, job.id)

        assert detail is not None
        assert detail.runner_token == "server"

    async def test_agent_job_reports_the_agent_uuid(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        job = Job(
            job_type=JobType.NETWORK_SCAN,
            agent_id=agent.id,
            params={},
            status="pending",
        )
        db.add(job)
        await db.flush()

        detail = await JobCoreService.get_job_detail(db, job.id)

        assert detail is not None
        assert detail.runner_token == str(agent.id)


async def test_detail_carries_the_duration_the_panel_shows(db: AsyncSession):
    """The panel reads job.duration_seconds; the detail DTO had no such field, so
    under StrictUndefined every job detail panel 500'd."""
    job = await _scan_job(db, result={**RAW_RESULT, "scan_duration_seconds": 12.5})

    detail = await JobCoreService.get_job_detail(db, job.id)

    assert detail is not None
    assert detail.duration_seconds == 12.5
