"""Notification log rows reach the view as typed entries, including a log whose
check has since been deleted (its check columns come back NULL from the outer
join, which the old untyped rows left for the template to discover)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.core.datetime_utils import utc_now
from app.models.enum_model import CheckType, NotificationStatus
from app.models.notification_log_model import NotificationLog
from app.services.core.notification_log_core_service import NotificationLogCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_check,
    make_notification_provider,
)
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


def _log(
    alert_id, provider_id, check_id, status=NotificationStatus.SENT
) -> NotificationLog:
    return NotificationLog(
        id=uuid4(),
        alert_id=alert_id,
        notification_provider_id=provider_id,
        check_result_id=uuid4(),
        check_result_timestamp=utc_now(),
        check_id=check_id,
        check_success=False,
        check_latency_ms=12.0,
        status=status,
        sent_at=utc_now(),
    )


async def test_entries_carry_the_check_or_none_when_it_is_gone(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    alert = make_alert(name="disk")
    provider = make_notification_provider(friendly_name="pager")
    db.add_all([agent, alert, provider])
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        display_name="web",
        check_type="http",
        target="https://x.test",
    )
    db.add(check)
    await db.flush()
    live = _log(alert.id, provider.id, check.id)
    orphan = _log(alert.id, provider.id, uuid4(), NotificationStatus.FAILED)
    db.add_all([live, orphan])
    await db.flush()

    entries, total = await NotificationLogCoreService.get_logs_paginated(db)

    assert total == 2
    by_id = {e.id: e for e in entries}
    assert by_id[live.id].display_name == "web"
    assert by_id[live.id].check_type == CheckType.HTTP
    assert by_id[live.id].alert_name == "disk"
    assert by_id[live.id].provider_name == "pager"
    assert by_id[orphan.id].display_name is None
    assert by_id[orphan.id].check_type is None
    assert by_id[orphan.id].status == NotificationStatus.FAILED

    single = await NotificationLogCoreService.get_log_by_id(db, orphan.id)
    assert single is not None and single.target is None
    assert await NotificationLogCoreService.get_log_by_id(db, uuid4()) is None
