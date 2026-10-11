"""The scheduler view builds its rows from DTOs the core converted in-session.

The core used to return ORM JobConfiguration/JobExecution rows from a
worker_session() that had already closed; it now hands SchedulerJobResponse /
SchedulerExecutionResponse, and the view's schedule and lease helpers read them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from app.models.enum_model import SchedulerJobCategory, SchedulerTriggerType
from app.models.scheduler_model import JobConfiguration
from app.schemas.scheduler_schema import SchedulerJobResponse
from app.services.views.scheduler_view_service import SchedulerViewService

pytestmark = pytest.mark.integration


class _Request:
    def url_for(self, name: str, **params: Any) -> SimpleNamespace:
        return SimpleNamespace(path=f"/{name}/{params.get('job_key', '')}")


async def _seed(maker, **overrides: Any) -> str:
    fields: dict[str, Any] = {
        "job_key": "view_job",
        "function_name": "test_noop",
        "display_name": "View Job",
        "category": SchedulerJobCategory.MONITORING,
        "trigger_type": SchedulerTriggerType.INTERVAL,
        "interval_seconds": 120,
        "enabled": True,
    }
    fields.update(overrides)
    async with maker() as s:
        s.add(JobConfiguration(**fields))
        await s.commit()
    return fields["job_key"]


async def test_list_toggle_and_history_rows(worker_db) -> None:
    key = await _seed(worker_db)

    listing = await SchedulerViewService.get_list_context(_Request())
    [row] = [r for r in listing["job_rows"] if r["job"].job_key == key]
    assert isinstance(row["job"], SchedulerJobResponse)
    assert row["schedule_display"] == "every 2m"
    assert row["is_running"] is False

    toggled = await SchedulerViewService.toggle_job(_Request(), key)
    assert toggled["job"].enabled is False

    history = await SchedulerViewService.get_job_history(key)
    assert history["job"].job_key == key
    assert history["execution_rows"] == []


async def test_a_held_lease_reads_as_running(worker_db) -> None:
    key = await _seed(
        worker_db,
        job_key="leased_job",
        lease_token=uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    listing = await SchedulerViewService.get_list_context(_Request())

    [row] = [r for r in listing["job_rows"] if r["job"].job_key == key]
    assert row["is_running"] is True
