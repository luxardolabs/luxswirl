"""The dashboard shows a silent check as "No data", not its last state.

A check's current result is its newest one within its silence threshold
(`app.core.liveness.silent_after`: 3 of its intervals plus a minute's grace).
Older than that, the row and the status filter both treat the check as
unknown (no data), while the row still says when it last reported. The
threshold follows each check's own interval, so an hourly check is current
long after a fixed window sized for minute checks would have dropped it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from app.core.datetime_utils import utc_now
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.enum_model import CheckHealthStatus
from app.services.views.status_view_service import StatusViewService
from fixtures.factories import make_agent, make_check, make_check_result
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _check(
    db: AsyncSession, agent: Agent, *, interval: int, ago: timedelta
) -> Check:
    check = make_check(agent_id=agent.id, interval_seconds=interval)
    db.add(check)
    await db.flush()
    db.add(
        make_check_result(
            check_id=check.id, agent_id=agent.id, timestamp=utc_now() - ago
        )
    )
    await db.flush()
    return check


@pytest_asyncio.fixture
async def agent(db: AsyncSession) -> Agent:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    return agent


async def test_a_silent_check_shows_no_data(db: AsyncSession, agent: Agent) -> None:
    check = await _check(db, agent, interval=60, ago=timedelta(minutes=10))

    [row], _ = await StatusViewService.get_all_checks_status(db, check_ids=[check.id])

    assert row.health == CheckHealthStatus.UNKNOWN
    assert row.status_text == "No data"
    assert row.latency_ms is None
    assert row.last_check is not None  # still says when it last reported


async def test_a_check_within_its_threshold_shows_its_state(
    db: AsyncSession, agent: Agent
) -> None:
    check = await _check(db, agent, interval=60, ago=timedelta(minutes=3))

    [row], _ = await StatusViewService.get_all_checks_status(db, check_ids=[check.id])

    assert row.health == CheckHealthStatus.UP


async def test_an_hourly_check_is_current_long_after_a_minute_check(
    db: AsyncSession, agent: Agent
) -> None:
    hourly = await _check(db, agent, interval=3600, ago=timedelta(minutes=50))

    [row], _ = await StatusViewService.get_all_checks_status(db, check_ids=[hourly.id])

    assert row.health == CheckHealthStatus.UP


@pytest.mark.parametrize(
    ("status", "expected"),
    [("up", {"current", "hourly"}), ("unknown", {"silent"}), ("down", set())],
)
async def test_the_status_filter_uses_the_same_rule(
    db: AsyncSession, agent: Agent, status: str, expected: set[str]
) -> None:
    checks = {
        "current": await _check(db, agent, interval=60, ago=timedelta(minutes=1)),
        "silent": await _check(db, agent, interval=60, ago=timedelta(minutes=10)),
        "hourly": await _check(db, agent, interval=3600, ago=timedelta(minutes=50)),
    }
    names = {c.id: name for name, c in checks.items()}

    rows, _ = await StatusViewService.get_all_checks_status(
        db, agent_id=agent.id, status=status
    )

    assert {names[r.check_id] for r in rows} == expected
