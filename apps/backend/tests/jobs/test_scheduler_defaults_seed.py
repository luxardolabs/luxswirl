"""Startup seeds the default scheduler jobs once, durably.

`init_scheduler_defaults` runs in the lifespan's worker session. Durability lock
for moving its inserts into crud (`fw.no_session_mutation_outside_crud`): after
the owning session commits, a SEPARATE connection sees every default job, and a
second run adds none.
"""

from __future__ import annotations

import pytest
from app.db import worker_session
from app.db.defaults.scheduler_templates import DEFAULT_JOB_CONFIGURATIONS
from app.db.scheduler_init import init_scheduler_defaults
from app.models.scheduler_model import JobConfiguration
from sqlalchemy import func, select

pytestmark = pytest.mark.integration


async def _stored_keys(maker) -> list[str]:
    async with maker() as other:  # separate connection: only committed rows
        rows = await other.scalars(select(JobConfiguration.job_key))
        return sorted(rows)


async def test_defaults_are_seeded_and_committed_once(worker_db) -> None:
    expected = sorted(c["job_key"] for c in DEFAULT_JOB_CONFIGURATIONS)

    async with worker_session() as db:
        await init_scheduler_defaults(db)
    assert await _stored_keys(worker_db) == expected

    async with worker_session() as db:
        await init_scheduler_defaults(db)
    async with worker_db() as other:
        total = await other.scalar(select(func.count()).select_from(JobConfiguration))
    assert total == len(expected)
