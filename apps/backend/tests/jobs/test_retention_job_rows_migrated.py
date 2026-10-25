"""Every existing job row that runs a retention cleanup still runs after 007.

The cleanup functions take no parameters now and the scheduler calls a job as
`func(**parameters)`, so a row still carrying `days_to_keep` would fail on
every run, and the scheduled path would disable the job after its retries,
leaving nothing to delete old artifacts or logs. Migration 007 clears the
parameters of every row that runs one of the functions, whatever they were.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.datetime_utils import utc_now
from app.models.check_artifact_model import CheckArtifact
from app.models.enum_model import (
    CheckArtifactType,
    SchedulerExecutionStatus,
    SchedulerJobCategory,
    SchedulerTriggerType,
)
from app.models.scheduler_model import JobConfiguration
from app.services.core.scheduler_core_service import SchedulerCoreService
from app.services.core.settings_core_service import SettingsCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import func, select

pytestmark = pytest.mark.integration

ARTIFACT_DAYS = "database.artifacts_retention_days"

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "007_retention_jobs_read_settings.py"
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("rev007rows", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection: Any, step: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), step)()


async def _upgrade(maker) -> None:
    async with maker() as s:
        await s.run_sync(lambda ss: _run(ss.connection(), "upgrade"))
        await s.commit()


async def _set(maker, key: str, value: int) -> None:
    async with maker() as s:
        if await SettingsCoreService.get_setting(s, key) is None:
            await SettingsCoreService.create_setting(
                s, key=key, category="database", value=value, display_name=key
            )
        else:
            await SettingsCoreService.update_setting(s, key, value)
        await s.commit()


async def _artifacts_aged(maker, *days_old: int) -> None:
    async with maker() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        check = make_check(agent_id=agent.id)
        s.add(check)
        await s.flush()
        for days in days_old:
            created = utc_now() - timedelta(days=days)
            s.add(
                CheckArtifact(
                    id=uuid4(),
                    created_at=created,
                    check_id=check.id,
                    check_result_id=uuid4(),
                    check_result_timestamp=created,
                    artifact_type=CheckArtifactType.SCREENSHOT,
                    content_type="image/png",
                    filename="shot.png",
                    size_bytes=1,
                    data=b"x",
                )
            )
        await s.commit()


async def _count(maker) -> int:
    async with maker() as s:
        return (
            await s.execute(select(func.count()).select_from(CheckArtifact))
        ).scalar_one()


async def _seed(maker, job_key: str, parameters: Any, **extra: Any) -> None:
    async with maker() as s:
        s.add(
            JobConfiguration(
                job_key=job_key,
                function_name="cleanup_check_artifacts",
                display_name=job_key,
                description="Delete check artifacts (screenshots, logs) older than 30 days",
                category=SchedulerJobCategory.CLEANUP,
                trigger_type=SchedulerTriggerType.CRON,
                cron_expression="30 3 * * *",
                enabled=True,
                parameters=parameters,
                **extra,
            )
        )
        await s.commit()


async def _get_job(maker, key: str) -> JobConfiguration:
    async with maker() as s:
        return (
            await s.execute(
                select(JobConfiguration).where(JobConfiguration.job_key == key)
            )
        ).scalar_one()


async def test_the_seeded_row_runs_after_the_migration(worker_db) -> None:
    await _set(worker_db, ARTIFACT_DAYS, 60)
    await _artifacts_aged(worker_db, 45, 61)
    await _seed(worker_db, "cleanup_check_artifacts", {"days_to_keep": 30})
    await _upgrade(worker_db)

    out = await SchedulerCoreService().execute_job_synchronously(
        "cleanup_check_artifacts"
    )

    assert out.status == SchedulerExecutionStatus.SUCCESS, out.error
    assert await _count(worker_db) == 1


@pytest.mark.parametrize(
    ("job_key", "parameters"),
    [
        # days set by hand, the only way to keep artifacts past 30 before the setting applied
        ("cleanup_check_artifacts", {"days_to_keep": 60}),
        # the seeded key beside another
        ("cleanup_check_artifacts", {"days_to_keep": 30, "note": "x"}),
        # a second job running the same function under another job_key
        ("artifacts_weekly", {"days_to_keep": 30}),
    ],
)
async def test_every_row_running_the_cleanup_runs_after_the_migration(
    worker_db, job_key: str, parameters: dict[str, Any]
) -> None:
    await _set(worker_db, ARTIFACT_DAYS, 60)
    await _seed(worker_db, job_key, parameters)
    await _upgrade(worker_db)

    out = await SchedulerCoreService().execute_job_synchronously(job_key)

    assert out.status == SchedulerExecutionStatus.SUCCESS, out.error
    assert (await _get_job(worker_db, job_key)).parameters is None


async def test_the_scheduled_path_keeps_the_job_enabled(worker_db) -> None:
    key = "cleanup_check_artifacts"
    await _set(worker_db, ARTIFACT_DAYS, 60)
    await _seed(
        worker_db,
        key,
        {"days_to_keep": 60},
        retry_limit=1,
        next_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await _upgrade(worker_db)
    svc = SchedulerCoreService()

    for _ in range(2):
        lease = uuid4()
        async with worker_db() as s:
            job = (
                await s.execute(
                    select(JobConfiguration).where(JobConfiguration.job_key == key)
                )
            ).scalar_one()
            job.lease_token = lease
            job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await s.commit()
        await svc._execute_job(key, lease)

    job = await _get_job(worker_db, key)
    assert job.enabled is True, f"retention job auto-disabled: {job.last_status}"
