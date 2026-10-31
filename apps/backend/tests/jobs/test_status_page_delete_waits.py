"""The status_page_delete job waits out an edit of the page in progress.

A request refuses a page another change holds (409), but the delete job runs
with no statement timeout and must not fail because an editor happened to be
saving: it takes the page's lock when the edit commits, and deletes it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from app.models.status_page_model import StatusPage
from app.services.core.status_page_core_service import StatusPageCoreService
from fixtures.factories import make_user
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _page_and_delete_job(maker: Any) -> tuple[UUID, UUID]:
    async with maker() as s:
        user = make_user(role="editor")
        s.add(user)
        page = StatusPage(name="p", slug=f"p-{uuid4().hex[:8]}", items=[])
        s.add(page)
        await s.flush()
        job = MaintenanceJob(
            id=uuid4(),
            kind=MaintenanceJobKind.STATUS_PAGE_DELETE,
            target_id=page.id,
            owner_id=user.id,
            status=MaintenanceJobStatus.QUEUED,
        )
        s.add(job)
        await s.commit()
        return page.id, job.id


async def test_the_delete_job_waits_for_an_edit_in_progress(worker_db: Any) -> None:
    page_id, job_id = await _page_and_delete_job(worker_db)
    register_all()
    locked = asyncio.Event()

    async def an_edit_in_progress() -> None:
        async with worker_db() as editor:
            await StatusPageCoreService.get_status_page_for_update(editor, page_id)
            locked.set()
            await asyncio.sleep(0.5)
            await editor.commit()

    edit = asyncio.create_task(an_edit_in_progress())
    await locked.wait()
    assert await process_next_job()
    await edit

    async with worker_db() as s:
        job = await s.scalar(select(MaintenanceJob).where(MaintenanceJob.id == job_id))
        page = await s.scalar(select(StatusPage).where(StatusPage.id == page_id))
    assert job is not None
    assert job.status == MaintenanceJobStatus.DONE, job.error
    assert page is None
