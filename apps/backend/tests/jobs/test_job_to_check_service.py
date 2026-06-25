"""Integration tests for JobToCheckCoreService.

Focused coverage (LUXSWIRL-127) on materializing monitoring checks from network
scan jobs: the single-check quick-action, the per-host bulk converter, duplicate
skipping, and the no-result error path.
"""

from __future__ import annotations

import pytest
from fixtures.factories import make_agent, make_check
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.job_schema import JobCreate
from app.services.core.check_core_service import CheckCoreService
from app.services.core.job_core_service import JobCoreService
from app.services.core.job_to_check_core_service import (
    BulkCheckParams,
    JobToCheckCoreService,
    QuickCheckParams,
)

svc = JobToCheckCoreService
pytestmark = pytest.mark.integration


async def _agent(db):
    a = make_agent()
    db.add(a)
    await db.flush()
    return a


async def _scan_job(db, result: dict):
    job = await JobCoreService.create_job(db, JobCreate(job_type="network_scan", params={}))
    job.result = result
    await db.flush()
    return job


class TestCreateSingleCheck:
    async def test_creates_check(self, db: AsyncSession):
        a = await _agent(db)
        ok, err = await svc.create_single_check(
            db,
            QuickCheckParams(
                agent_id=a.id,
                check_type="ping",
                target="10.0.0.5",
                display_name="ping_host5",
                interval=60,
                timeout=10,
                retry_attempts=1,
                tags=[],
            ),
        )
        assert ok is True
        assert err is None
        checks = await CheckCoreService.list_checks_for_agent(db, a.id)
        assert any(c.display_name == "ping_host5" for c in checks)

    async def test_duplicate_name_returns_false(self, db: AsyncSession):
        a = await _agent(db)
        db.add(make_check(agent_id=a.id, display_name="dup_check"))
        await db.flush()
        ok, err = await svc.create_single_check(
            db,
            QuickCheckParams(
                agent_id=a.id,
                check_type="ping",
                target="10.0.0.9",
                display_name="dup_check",
                interval=60,
                timeout=10,
                retry_attempts=1,
                tags=[],
            ),
        )
        assert ok is False
        assert err == "Check already exists"


class TestCreatePingChecksFromJob:
    @staticmethod
    def _params(agent_id):
        return BulkCheckParams(
            interval=60, timeout=10, retry_attempts=1, agent_id=agent_id, tags=[]
        )

    async def test_one_check_per_host(self, db: AsyncSession):
        a = await _agent(db)
        job = await _scan_job(db, {"discovered_hosts": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]})
        res, err = await svc.create_ping_checks_from_job(db, job.id, self._params(a.id))
        assert err is None
        assert res.created_count == 2

    async def test_skips_existing_host(self, db: AsyncSession):
        a = await _agent(db)
        # ping_<ip-with-dots-as-underscores>; 10.0.0.1 -> ping_10_0_0_1
        db.add(make_check(agent_id=a.id, display_name="ping_10_0_0_1"))
        await db.flush()
        job = await _scan_job(db, {"discovered_hosts": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]})
        res, err = await svc.create_ping_checks_from_job(db, job.id, self._params(a.id))
        assert res.created_count == 1
        assert res.skipped_count == 1

    async def test_no_discovered_hosts_is_error(self, db: AsyncSession):
        a = await _agent(db)
        job = await _scan_job(db, {})  # result present but no discovered_hosts
        res, err = await svc.create_ping_checks_from_job(db, job.id, self._params(a.id))
        assert res is None
        assert err is not None

    async def test_partial_host_with_no_ip_or_hostname(self, db: AsyncSession):
        # ADVERSARIAL: a real scan can return a host with neither ip nor hostname.
        # Robust behavior = create the good host, skip the junk one — NOT crash the
        # whole batch (which would lose the good hosts too).
        a = await _agent(db)
        job = await _scan_job(db, {"discovered_hosts": [{"ip": "10.0.0.1"}, {}]})
        res, err = await svc.create_ping_checks_from_job(db, job.id, self._params(a.id))
        assert err is None
        assert res.created_count == 1  # the good host
        assert res.skipped_count == 1  # the junk host, skipped not crashed
