"""Integration tests for JobToCheckCoreService.

Focused coverage on materializing monitoring checks from network
scan jobs: the single-check quick-action, the per-host bulk converter, duplicate
skipping, the no-result error path, which jobs checks are made from, the cap
on one request, the agent-written host fields, and two requests at once.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import pytest
from app.models.check_model import Check
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.schemas.job_schema import JobCreate
from app.services.core import job_to_check_core_service
from app.services.core.check_core_service import CheckCoreService
from app.services.core.job_core_service import JobCoreService
from app.services.core.job_to_check_core_service import (
    BulkCheckParams,
    JobToCheckCoreService,
    QuickCheckParams,
)
from app.services.core.network_scan_core_service import NetworkScanCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

svc = JobToCheckCoreService
pytestmark = pytest.mark.integration


async def _agent(db):
    a = make_agent()
    db.add(a)
    await db.flush()
    return a


async def _scan_job(db, result: dict):
    job = await JobCoreService.create_job(
        db,
        JobCreate(
            job_type="network_scan",
            agent_id=(await _agent(db)).id,
            params={"subnet": "192.168.1.0/24"},
        ),
    )
    job.result = result
    job.status = JobStatus.COMPLETED
    await db.flush()
    return job


def _params(agent_id):
    return BulkCheckParams(
        interval=60, timeout=10, retry_attempts=1, agent_id=agent_id, tags=[]
    )


CREATORS = {
    "ping": svc.create_ping_checks_from_job,
    "web": svc.create_web_checks_from_job,
    "ssh": svc.create_ssh_checks_from_job,
    "database": svc.create_database_checks_from_job,
}

SCAN = {
    "discovered_hosts": [
        {"ip": "10.1.2.3", "hostname": None, "open_ports": [22, 80, 5432]},
    ],
    "scan_params": {"ports": [22, 80, 5432]},
}


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
    async def test_one_check_per_host(self, db: AsyncSession):
        a = await _agent(db)
        job = await _scan_job(
            db, {"discovered_hosts": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]}
        )
        res, err = await svc.create_ping_checks_from_job(db, job.id, _params(a.id))
        assert err is None
        assert res.created_count == 2

    async def test_skips_existing_host(self, db: AsyncSession):
        a = await _agent(db)
        # ping_<ip-with-dots-as-underscores>; 10.0.0.1 -> ping_10_0_0_1
        db.add(make_check(agent_id=a.id, display_name="ping_10_0_0_1"))
        await db.flush()
        job = await _scan_job(
            db, {"discovered_hosts": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]}
        )
        res, err = await svc.create_ping_checks_from_job(db, job.id, _params(a.id))
        assert res.created_count == 1
        assert res.skipped_count == 1

    async def test_no_discovered_hosts_is_error(self, db: AsyncSession):
        a = await _agent(db)
        job = await _scan_job(db, {})  # result present but no discovered_hosts
        res, err = await svc.create_ping_checks_from_job(db, job.id, _params(a.id))
        assert res is None
        assert err is not None

    async def test_partial_host_with_no_ip_or_hostname(self, db: AsyncSession):
        # ADVERSARIAL: a real scan can return a host with neither ip nor hostname.
        # Robust behavior = create the good host, skip the junk one — NOT crash the
        # whole batch (which would lose the good hosts too).
        a = await _agent(db)
        job = await _scan_job(db, {"discovered_hosts": [{"ip": "10.0.0.1"}, {}]})
        res, err = await svc.create_ping_checks_from_job(db, job.id, _params(a.id))
        assert err is None
        assert res.created_count == 1  # the good host
        assert res.skipped_count == 1  # the junk host, skipped not crashed


class TestOnlyACompletedScanMakesChecks:
    @pytest.mark.parametrize("kind", sorted(CREATORS))
    @pytest.mark.parametrize(
        ("job_type", "status"),
        [
            (JobType.NETWORK_DISCOVER, JobStatus.COMPLETED),
            (JobType.NETWORK_SCAN, JobStatus.FAILED),
            (JobType.NETWORK_SCAN, JobStatus.PENDING),
            (JobType.NETWORK_SCAN, JobStatus.RUNNING),
        ],
    )
    async def test_other_jobs_are_refused_and_make_nothing(
        self, db: AsyncSession, kind, job_type, status
    ):
        a = await _agent(db)
        job = await _scan_job(db, SCAN)
        job.job_type = job_type
        job.status = status
        await db.flush()

        res, err = await CREATORS[kind](db, job.id, _params(a.id))

        assert res is None
        assert err is not None
        assert list(await CheckCoreService.list_checks_for_agent(db, a.id)) == []

    @pytest.mark.parametrize("kind", sorted(CREATORS))
    async def test_a_completed_scan_makes_its_checks(self, db: AsyncSession, kind):
        a = await _agent(db)
        job = await _scan_job(db, SCAN)

        res, err = await CREATORS[kind](db, job.id, _params(a.id))

        assert err is None
        assert res.created_count == 1


class TestTheCapOnOneRequest:
    async def test_a_request_past_the_cap_makes_nothing(
        self, db: AsyncSession, monkeypatch
    ):
        monkeypatch.setattr(job_to_check_core_service, "BULK_MAX", 2, raising=False)
        a = await _agent(db)
        job = await _scan_job(
            db, {"discovered_hosts": [{"ip": f"10.0.0.{i}"} for i in range(1, 4)]}
        )

        res, err = await svc.create_ping_checks_from_job(db, job.id, _params(a.id))

        made = await CheckCoreService.list_checks_for_agent(db, a.id)
        assert (res, len(made)) == (None, 0), f"{len(made)} checks past a cap of 2"
        assert err is not None and "2" in err

    async def test_the_cap_counts_only_the_checks_it_would_make(
        self, db: AsyncSession, monkeypatch
    ):
        monkeypatch.setattr(job_to_check_core_service, "BULK_MAX", 2, raising=False)
        a = await _agent(db)
        db.add(make_check(agent_id=a.id, display_name="ping_10_0_0_1"))
        await db.flush()
        job = await _scan_job(
            db, {"discovered_hosts": [{"ip": f"10.0.0.{i}"} for i in range(1, 4)]}
        )

        res, err = await svc.create_ping_checks_from_job(db, job.id, _params(a.id))

        assert err is None
        assert (res.created_count, res.skipped_count) == (2, 1)


class TestAgentWrittenHostFields:
    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            (
                {"ip": "10.1.2.3", "hostname": "10.1.2.3@10.9.9.9"},
                {"ip": "10.1.2.3", "hostname": None},
            ),
            (
                {"ip": "10.1.2.3", "hostname": "evil/path"},
                {"ip": "10.1.2.3", "hostname": None},
            ),
            (
                {"ip": "10.1.2.3", "hostname": "a:99"},
                {"ip": "10.1.2.3", "hostname": None},
            ),
            (
                {"ip": "10.1.2.3", "hostname": "-x"},
                {"ip": "10.1.2.3", "hostname": None},
            ),
            (
                {"ip": "10.1.2.3", "hostname": "a" * 64 + ".lan"},
                {"ip": "10.1.2.3", "hostname": None},
            ),
            ({"ip": "10.1.2.3", "hostname": 7}, {"ip": "10.1.2.3", "hostname": None}),
            (
                {"ip": "10.1.2.3", "hostname": "nas-01.home.lan"},
                {"ip": "10.1.2.3", "hostname": "nas-01.home.lan"},
            ),
            (
                {"ip": "10.1.2.3", "hostname": "WIN_PC"},
                {"ip": "10.1.2.3", "hostname": "WIN_PC"},
            ),
            ({"ip": "10.9.9.9@x", "hostname": None}, {"ip": None, "hostname": None}),
            ({"ip": "fd00::1"}, {"ip": "fd00::1", "hostname": None}),
        ],
        ids=[
            "userinfo",
            "path",
            "port",
            "leading-hyphen",
            "label-too-long",
            "not-a-string",
            "dns-name",
            "underscore",
            "ip-not-an-ip",
            "ipv6",
        ],
    )
    def test_scan_hosts_keeps_only_an_ip_and_a_hostname(self, host, expected):
        (got,) = NetworkScanCoreService.scan_hosts({"discovered_hosts": [host]})
        assert {"ip": got.get("ip"), "hostname": got.get("hostname")} == expected

    async def test_a_forged_hostname_cannot_point_a_check_elsewhere(
        self, db: AsyncSession
    ):
        a = await _agent(db)
        job = await _scan_job(
            db,
            {
                "discovered_hosts": [
                    {
                        "ip": "10.1.2.3",
                        "hostname": "10.1.2.3@10.9.9.9",
                        "open_ports": [80],
                    }
                ],
                "scan_params": {"ports": [80]},
            },
        )

        await svc.create_web_checks_from_job(db, job.id, _params(a.id))

        targets = [
            c.target for c in await CheckCoreService.list_checks_for_agent(db, a.id)
        ]
        assert {urlsplit(t).hostname for t in targets} == {"10.1.2.3"}, targets


async def test_two_requests_at_once_make_each_check_once(worker_db, monkeypatch):
    """Both requests read the job before either writes: the second waits for
    the first's checks and skips them."""
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        job = Job(
            job_type=JobType.NETWORK_SCAN,
            agent_id=agent.id,
            params={"subnet": "10.1.2.0/24"},
            status=JobStatus.COMPLETED,
            priority=0,
            result={"discovered_hosts": [{"ip": f"10.1.2.{i}"} for i in range(1, 21)]},
        )
        s.add(job)
        await s.commit()
        agent_id, job_id = agent.id, job.id

    original = JobCoreService.get_job
    arrived = 0
    both = asyncio.Event()

    async def read_then_wait(db, jid):
        nonlocal arrived
        row = await original(db, jid)
        arrived += 1
        if arrived >= 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        return row

    monkeypatch.setattr(JobCoreService, "get_job", staticmethod(read_then_wait))

    async def create():
        async with worker_db() as s:
            res, err = await svc.create_ping_checks_from_job(
                s, job_id, _params(agent_id)
            )
            await s.commit()
            assert err is None
            return res.created_count, res.skipped_count

    results = await asyncio.gather(create(), create())
    monkeypatch.setattr(JobCoreService, "get_job", staticmethod(original))

    async with worker_db() as s:
        names = list(
            (
                await s.scalars(
                    select(Check.display_name).where(Check.agent_id == agent_id)
                )
            ).all()
        )
    assert len(names) == len(set(names)) == 20, (
        f"{len(names)} checks for 20 hosts, {len(names) - len(set(names))} duplicates"
    )
    assert sorted(results) == [(0, 20), (20, 0)]
