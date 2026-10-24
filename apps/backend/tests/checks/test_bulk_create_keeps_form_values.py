"""A bulk-created check keeps the values the bulk form posted.

The form's values become one `BulkCheckCreateRequest` per URL, which the
background job stores as JSON and rebuilds before creating each check. A value
the form posts must survive all three steps.
"""

from __future__ import annotations

import pytest
from app.models.check_model import Check
from app.schemas.check_schema import BulkCheckCreateRequest
from app.services.core.check_core_service import CheckCoreService
from app.services.views.checks_view_service import ChecksViewService
from fixtures.factories import make_agent
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _bulk_create(db: AsyncSession, **form: str) -> list[Check]:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    requests = ChecksViewService.build_bulk_requests(
        ["https://127.0.0.1/a", "https://127.0.0.1/b"],
        ["a", "b"],
        interval=60,
        enabled=True,
        **form,
    )
    # As the job stores and rebuilds them.
    queued = [BulkCheckCreateRequest(**r.model_dump(mode="json")) for r in requests]

    await CheckCoreService.bulk_create_checks(db, agent.id, queued)

    return list(await CheckCoreService.list_checks_for_agent(db, agent.id))


async def test_resend_notification_after_reaches_every_check(db: AsyncSession) -> None:
    checks = await _bulk_create(db, resend_notification_after="3")

    assert [c.resend_notification_after for c in checks] == [3, 3]


async def test_a_blank_resend_value_leaves_it_off(db: AsyncSession) -> None:
    checks = await _bulk_create(db, resend_notification_after="")

    assert [c.resend_notification_after for c in checks] == [None, None]


def test_a_job_queued_without_the_field_still_rebuilds() -> None:
    """A job stored before the field existed has no key for it."""
    assert (
        BulkCheckCreateRequest(url="https://127.0.0.1/").resend_notification_after
        is None
    )
