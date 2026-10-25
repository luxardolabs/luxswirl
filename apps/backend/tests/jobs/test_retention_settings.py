"""The retention settings decide how long artifacts and notification logs stay.

The scheduler jobs `cleanup_check_artifacts` and `cleanup_notification_logs`
read `database.artifacts_retention_days` and
`database.notification_logs_retention_days` (0 keeps every log); the database
maintenance cycle deletes neither. So raising a retention setting keeps the
rows that long, and the scheduler page and the settings page agree.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.datetime_utils import utc_now
from app.core.default_settings import DEFAULT_SETTINGS
from app.db import database as _database
from app.db.defaults.scheduler_templates import DEFAULT_JOB_CONFIGURATIONS
from app.models.check_artifact_model import CheckArtifact
from app.models.enum_model import (
    CheckArtifactType,
    SchedulerJobCategory,
    SchedulerTriggerType,
)
from app.models.notification_log_model import NotificationLog
from app.models.scheduler_model import JobConfiguration
from app.services.core.cleanup_core_service import (
    cleanup_check_artifacts,
    cleanup_notification_logs,
)
from app.services.core.database_maintenance_core_service import (
    DatabaseMaintenanceCoreService,
)
from app.services.core.settings_core_service import SettingsCoreService
from fixtures._db_url import TEST_DATABASE_URL
from fixtures.factories import (
    make_agent,
    make_alert,
    make_check,
    make_notification_log,
    make_notification_provider,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

pytestmark = pytest.mark.integration

ARTIFACT_DAYS = "database.artifacts_retention_days"
LOG_DAYS = "database.notification_logs_retention_days"


@pytest_asyncio.fixture
async def app_engine(worker_db) -> AsyncIterator[AsyncEngine]:
    """The global app engine bound to the test DB, for the maintenance cycle's
    AUTOCOMMIT steps (VACUUM, aggregate refresh)."""
    engine = create_async_engine(TEST_DATABASE_URL)
    saved = _database.engine
    _database.engine = engine
    try:
        yield engine
    finally:
        _database.engine = saved
        await engine.dispose()


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


async def _logs_aged(maker, *days_old: int) -> None:
    async with maker() as s:
        alert = make_alert()
        provider = make_notification_provider()
        s.add_all([alert, provider])
        await s.flush()
        for days in days_old:
            s.add(
                make_notification_log(
                    alert_id=alert.id,
                    notification_provider_id=provider.id,
                    check_result_id=None,
                    check_result_timestamp=None,
                    sent_at=utc_now() - timedelta(days=days),
                )
            )
        await s.commit()


async def _count(maker, model: Any) -> int:
    async with maker() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_raising_artifact_retention_keeps_older_artifacts(
    worker_db, app_engine
) -> None:
    await _set(worker_db, ARTIFACT_DAYS, 60)
    await _artifacts_aged(worker_db, 45, 61)

    out = await cleanup_check_artifacts()
    await DatabaseMaintenanceCoreService.run_cycle()

    assert out["deleted"] == 1 and out["days_to_keep"] == 60
    assert await _count(worker_db, CheckArtifact) == 1


async def test_lowering_artifact_retention_deletes_sooner(worker_db) -> None:
    await _set(worker_db, ARTIFACT_DAYS, 3)
    await _artifacts_aged(worker_db, 1, 5)

    out = await cleanup_check_artifacts()

    assert out["deleted"] == 1
    assert await _count(worker_db, CheckArtifact) == 1


async def test_the_maintenance_cycle_deletes_no_artifacts_or_logs(
    worker_db, app_engine
) -> None:
    await _set(worker_db, ARTIFACT_DAYS, 1)
    await _set(worker_db, LOG_DAYS, 1)
    await _artifacts_aged(worker_db, 30)
    await _logs_aged(worker_db, 30)

    await DatabaseMaintenanceCoreService.run_cycle()

    assert await _count(worker_db, CheckArtifact) == 1
    assert await _count(worker_db, NotificationLog) == 1


async def test_notification_logs_follow_their_retention_setting(worker_db) -> None:
    await _set(worker_db, LOG_DAYS, 45)
    await _logs_aged(worker_db, 40, 50)

    out = await cleanup_notification_logs()

    assert out["deleted"] == 1 and out["days_to_keep"] == 45
    assert await _count(worker_db, NotificationLog) == 1


async def test_zero_keeps_every_notification_log(worker_db) -> None:
    await _set(worker_db, LOG_DAYS, 0)
    await _logs_aged(worker_db, 400)

    out = await cleanup_notification_logs()

    assert out["deleted"] == 0
    assert await _count(worker_db, NotificationLog) == 1


@pytest.mark.unit
def test_the_notification_log_retention_setting_is_seeded() -> None:
    (entry,) = [e for e in DEFAULT_SETTINGS if e["key"] == LOG_DAYS]

    assert entry["value"] == 90
    assert entry["validation"]["min"] == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "job_key", ["cleanup_check_artifacts", "cleanup_notification_logs"]
)
def test_the_retention_jobs_carry_no_days_of_their_own(job_key: str) -> None:
    (job,) = [j for j in DEFAULT_JOB_CONFIGURATIONS if j["job_key"] == job_key]

    assert "days_to_keep" not in (job.get("parameters") or {})
    assert "30 days" not in job["description"]


# --- existing job rows lose the days they were seeded with -------------------

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "007_retention_jobs_read_settings.py"
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("rev007", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection: Any, step: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), step)()


@pytest.mark.parametrize(
    "job_key", ["cleanup_check_artifacts", "cleanup_notification_logs"]
)
async def test_the_migration_drops_the_seeded_days_from_existing_jobs(
    db: AsyncSession, job_key: str
) -> None:
    row = JobConfiguration(
        job_key=job_key,
        function_name=job_key,
        display_name=job_key,
        description=_migration().OLD_DESCRIPTIONS[job_key],
        category=SchedulerJobCategory.CLEANUP,
        trigger_type=SchedulerTriggerType.CRON,
        cron_expression="30 3 * * *",
        enabled=True,
        parameters={"days_to_keep": 30},
    )
    db.add(row)
    await db.flush()

    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))
    await db.refresh(row)
    upgraded = (row.parameters, row.description)
    await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    await db.refresh(row)

    assert upgraded == (None, _migration().NEW_DESCRIPTIONS[job_key])
    assert row.parameters == {"days_to_keep": 30}
    assert row.description == _migration().OLD_DESCRIPTIONS[job_key]
