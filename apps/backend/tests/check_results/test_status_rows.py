"""Status-dashboard rows come from slim DTOs: no check config, no secrets.

The status view reads a check's identity and labels plus each recent result's
(success, latency) for the minute bars. It no longer receives ORM check/agent
rows, and never decrypts a database check's connection string to draw a row.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.datetime_utils import utc_now
from app.services.views.status_view_service import StatusViewService
from fixtures.factories import make_agent, make_check, make_check_result
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_a_row_with_its_minute_bars(db: AsyncSession) -> None:
    agent = make_agent(agent_name="edge", tags=["prod"])
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, display_name="api", tags=["web"])
    db.add(check)
    await db.flush()
    db.add_all(
        [
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                success=minutes != 2,
                latency_ms=100.0,
                timestamp=utc_now() - timedelta(minutes=minutes, seconds=10),
            )
            for minutes in range(4)
        ]
    )
    await db.flush()

    rows, total = await StatusViewService.get_all_checks_status(
        db, check_ids=[check.id], include_minute_bars=True
    )

    assert total == 1
    [row] = rows
    assert row.check_name == "api"
    assert len(row.minute_bars) == 15
    # Oldest first: the last four bars are 3, 2, 1 and 0 minutes ago.
    assert [b["success"] for b in row.minute_bars[-4:]] == [True, False, True, True]
    assert all(b["success"] is None for b in row.minute_bars[:-4])
