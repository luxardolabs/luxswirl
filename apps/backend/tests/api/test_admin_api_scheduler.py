"""The scheduler on the admin API: an admin's alone.

The scheduler commits through its own sessions, so every test here runs on
really-committing sessions (`worker_db`) and reads what landed back from a
fresh one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.enum_model import (
    SchedulerExecutionStatus,
    SchedulerJobCategory,
    SchedulerTriggerType,
)
from app.models.scheduler_model import JobConfiguration, JobExecution
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.scheduler_core_service import scheduler_service
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

pytestmark = pytest.mark.integration

JOBS = "/api/v1/admin/scheduler/jobs"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


@pytest.fixture
def test_function(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Register `test_noop` on the app's scheduler; every call's kwargs, in order."""
    calls: list[dict] = []

    async def _noop(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setitem(scheduler_service.job_functions, "test_noop", _noop)
    return calls


async def _as(maker, role: str) -> dict[str, str]:
    async with maker() as s:
        user = make_user(role=role)
        s.add(user)
        await s.flush()
        _, plaintext = await ApiTokenCoreService.create_token(
            s, user.id, ApiTokenCreate(name=role)
        )
        await s.commit()
    return {"Authorization": f"Bearer {plaintext}"}


async def _seed_job(maker, **overrides) -> str:
    fields: dict = {
        "job_key": "test_job",
        "function_name": "test_noop",
        "display_name": "Test Job",
        "category": SchedulerJobCategory.MONITORING,
        "trigger_type": SchedulerTriggerType.INTERVAL,
        "interval_seconds": 60,
        "enabled": True,
    }
    fields.update(overrides)
    async with maker() as s:
        s.add(JobConfiguration(**fields))
        await s.commit()
    return fields["job_key"]


async def _job(maker, key: str) -> JobConfiguration:
    async with maker() as s:
        return (
            await s.execute(
                select(JobConfiguration).where(JobConfiguration.job_key == key)
            )
        ).scalar_one()


# ── Who may ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["viewer", "editor"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", ""),
        ("GET", "/test_job"),
        ("GET", "/test_job/history"),
        ("POST", "/test_job/enable"),
        ("POST", "/test_job/disable"),
        ("POST", "/test_job/run"),
        ("POST", "/test_job/reset"),
    ],
)
async def test_only_an_admin_reaches_the_scheduler(
    worker_db,
    client: AsyncClient,
    test_function: list[dict],
    role: str,
    method: str,
    path: str,
) -> None:
    key = await _seed_job(worker_db, enabled=False, failed_runs=2)
    other = await _as(worker_db, role)

    resp = await client.request(method, JOBS + path, headers=other)

    assert resp.status_code == 403, resp.text
    job = await _job(worker_db, key)
    assert job.enabled is False and job.failed_runs == 2 and job.total_runs == 0
    assert test_function == []


async def test_no_token_is_refused(client: AsyncClient) -> None:
    assert (await client.get(JOBS)).status_code == 401


# ── Reads ───────────────────────────────────────────────────────────────────


async def test_an_admin_lists_and_reads_jobs_and_their_history(
    worker_db, client: AsyncClient
) -> None:
    key = await _seed_job(worker_db)
    async with worker_db() as s:
        for minutes in (3, 2, 1):
            s.add(
                JobExecution(
                    id=uuid4(),
                    job_key=key,
                    job_name="Test Job",
                    category=SchedulerJobCategory.MONITORING,
                    started_at=datetime.now(UTC) - timedelta(minutes=minutes),
                    status=SchedulerExecutionStatus.SUCCESS,
                )
            )
        await s.commit()
    admin = await _as(worker_db, "admin")

    listed = await client.get(JOBS, headers=admin)
    one = await client.get(f"{JOBS}/{key}", headers=admin)
    history = await client.get(
        f"{JOBS}/{key}/history", params={"limit": 2}, headers=admin
    )

    assert key in [j["job_key"] for j in listed.json()]
    assert one.json()["display_name"] == "Test Job"
    started = [e["started_at"] for e in history.json()]
    assert len(started) == 2 and started == sorted(started, reverse=True)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/nope"),
        ("GET", "/nope/history"),
        ("POST", "/nope/enable"),
        ("POST", "/nope/disable"),
        ("POST", "/nope/run"),
        ("POST", "/nope/reset"),
    ],
)
async def test_an_unknown_job_is_404(
    worker_db, client: AsyncClient, method: str, path: str
) -> None:
    admin = await _as(worker_db, "admin")

    resp = await client.request(method, JOBS + path, headers=admin)

    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/scheduler/jobs/nope/toggle"),
        ("POST", "/scheduler/jobs/nope/run"),
        ("POST", "/scheduler/jobs/nope/reset"),
        ("GET", "/scheduler/jobs/nope/history"),
    ],
)
async def test_the_web_answers_an_unknown_job_with_404(
    worker_db, client: AsyncClient, method: str, path: str
) -> None:
    async with worker_db() as s:
        user = make_user(role="admin")
        s.add(user)
        await s.flush()
        _, session_token = await AuthCoreService().create_session(s, user.id)
        await s.commit()
    client.cookies.set(settings.security.session_cookie_name, session_token)

    resp = await client.request(method, path, headers={"HX-Request": "true"})

    assert resp.status_code == 404, resp.text
    assert "not found" in (resp.headers.get("HX-Trigger", "") + resp.text)


# ── Changes ─────────────────────────────────────────────────────────────────


async def test_enable_and_disable_set_the_state_and_repeat_harmlessly(
    worker_db, client: AsyncClient
) -> None:
    stale = datetime.now(UTC) - timedelta(days=3)
    key = await _seed_job(worker_db, enabled=False, next_run_at=stale)
    admin = await _as(worker_db, "admin")

    first = await client.post(f"{JOBS}/{key}/enable", headers=admin)
    again = await client.post(f"{JOBS}/{key}/enable", headers=admin)
    enabled = await _job(worker_db, key)
    off = await client.post(f"{JOBS}/{key}/disable", headers=admin)
    off_again = await client.post(f"{JOBS}/{key}/disable", headers=admin)

    assert first.json()["enabled"] is True and again.json()["enabled"] is True
    assert enabled.next_run_at is not None and enabled.next_run_at > stale
    assert off.json()["enabled"] is False and off_again.json()["enabled"] is False
    assert (await _job(worker_db, key)).enabled is False


async def test_a_run_now_runs_the_job_and_records_it(
    worker_db, client: AsyncClient, test_function: list[dict]
) -> None:
    key = await _seed_job(worker_db, parameters={"days": 3})
    admin = await _as(worker_db, "admin")

    resp = await client.post(f"{JOBS}/{key}/run", headers=admin)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"
    assert test_function == [{"days": 3}]
    assert (await _job(worker_db, key)).total_runs == 1
    async with worker_db() as s:
        runs = await s.scalar(
            select(func.count())
            .select_from(JobExecution)
            .where(JobExecution.job_key == key)
        )
    assert runs == 1


async def test_a_failed_run_is_answered_with_its_error(
    worker_db, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(**_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setitem(scheduler_service.job_functions, "test_noop", _boom)
    key = await _seed_job(worker_db)
    admin = await _as(worker_db, "admin")

    resp = await client.post(f"{JOBS}/{key}/run", headers=admin)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed" and resp.json()["error"] == "disk full"
    assert (await _job(worker_db, key)).failed_runs == 1


async def test_a_reset_clears_failures_and_turns_the_job_on(
    worker_db, client: AsyncClient
) -> None:
    key = await _seed_job(worker_db, enabled=False, retry_count=3, failed_runs=2)
    admin = await _as(worker_db, "admin")

    resp = await client.post(f"{JOBS}/{key}/reset", headers=admin)

    assert resp.status_code == 200, resp.text
    job = await _job(worker_db, key)
    assert job.enabled is True and job.retry_count == 0 and job.failed_runs == 0


# ── One run at a time ───────────────────────────────────────────────────────


async def test_a_run_while_another_holds_the_lease_is_refused(
    worker_db, client: AsyncClient, test_function: list[dict]
) -> None:
    key = await _seed_job(
        worker_db,
        lease_token=uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    admin = await _as(worker_db, "admin")

    resp = await client.post(f"{JOBS}/{key}/run", headers=admin)

    assert resp.status_code == 409, resp.text
    assert test_function == []


async def test_a_run_by_hand_during_a_scheduled_run_leaves_its_count_whole(
    worker_db, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, gate = asyncio.Event(), asyncio.Event()
    calls: list[int] = []

    async def _slow(**_kwargs):
        calls.append(1)
        entered.set()
        await gate.wait()
        return {"ok": True}

    monkeypatch.setitem(scheduler_service.job_functions, "test_noop", _slow)
    lease = uuid4()
    key = await _seed_job(
        worker_db,
        lease_token=lease,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    admin = await _as(worker_db, "admin")
    scheduled = asyncio.create_task(scheduler_service._execute_job(key, lease))
    await asyncio.wait_for(entered.wait(), 10)

    manual = await client.post(f"{JOBS}/{key}/run", headers=admin)
    gate.set()
    await asyncio.wait_for(scheduled, 10)

    assert manual.status_code == 409, manual.text
    assert calls == [1]
    job = await _job(worker_db, key)
    assert job.total_runs == 1 and job.lease_token is None


async def test_a_run_by_hand_frees_the_job_when_it_ends(
    worker_db, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = iter([RuntimeError("disk full"), None])

    async def _flaky(**_kwargs):
        error = next(outcomes)
        if error is not None:
            raise error
        return {"ok": True}

    monkeypatch.setitem(scheduler_service.job_functions, "test_noop", _flaky)
    key = await _seed_job(worker_db)
    admin = await _as(worker_db, "admin")

    failed = await client.post(f"{JOBS}/{key}/run", headers=admin)
    succeeded = await client.post(f"{JOBS}/{key}/run", headers=admin)

    assert failed.json()["status"] == "failed"
    assert succeeded.status_code == 200 and succeeded.json()["status"] == "success"
    job = await _job(worker_db, key)
    assert job.lease_token is None and job.lease_expires_at is None
