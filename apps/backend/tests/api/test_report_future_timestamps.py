"""POST /api/v1/agent/reports refuses a result timestamped in the future.

A result's timestamp is the agent's clock. Every reader of a check's status
bounds results from below only: the newest result is the current one
(dashboard, admin API, status filter), the newest few are what a
consecutive-failure alert counts. A success stamped a year ahead would stay
the check's current result for a year, whatever is reported after it, on a
replicate check whatever every OTHER agent reports. So a result later than the
server's clock plus `RESULT_CLOCK_TOLERANCE` is counted in `results_failed`
and not stored; one within the tolerance (an agent clock a little fast) is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.crud.alert_crud import AlertCRUD
from app.main import app
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.enum_model import CheckHealthStatus
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService
from app.services.views.status_view_service import StatusViewService
from fixtures.factories import make_agent, make_alert, make_check
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _approved(db: AsyncSession, **overrides: Any) -> tuple[Agent, str]:
    agent = make_agent(approval_status="pending", **overrides)
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)
    return agent, key


def _result(check_id: UUID, *, success: bool, at: datetime) -> dict[str, Any]:
    return {
        "timestamp": at.isoformat(),
        "success": success,
        "latency_ms": 5.0,
        "error": None if success else "connection refused",
        "check_id": str(check_id),
        "display_name": "probe",
        "check_type": "ping",
        "target": "192.0.2.10",
    }


async def _post(
    client: AsyncClient, agent: Agent, key: str, *results: dict[str, Any]
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/agent/reports",
        json={
            "agent_id": str(agent.id),
            "timestamp": utc_now().isoformat(),
            "checks": list(results),
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 202, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _replicate(db: AsyncSession) -> tuple[tuple[Agent, str], ...]:
    honest = await _approved(db, tags=["role:probe"])
    rogue = await _approved(db, tags=["role:probe"])
    return honest, rogue


async def _replicate_check(db: AsyncSession, owner: Agent) -> Check:
    check = make_check(
        agent_id=owner.id,
        assignment_mode="replicate",
        agent_selector={"tags": ["role:probe"]},
        interval_seconds=60,
    )
    db.add(check)
    await db.flush()
    return check


async def _failures(
    client: AsyncClient, agent: Agent, key: str, check: Check, n: int = 3
) -> None:
    now = utc_now()
    body = await _post(
        client,
        agent,
        key,
        *[
            _result(check.id, success=False, at=now - timedelta(seconds=10 * i))
            for i in range(n)
        ],
    )
    assert body["results_processed"] == n, body


async def _health(db: AsyncSession, check_id: UUID) -> CheckHealthStatus:
    [row], _ = await StatusViewService.get_all_checks_status(db, check_ids=[check_id])
    return row.health


async def test_a_future_result_is_counted_failed_and_not_stored(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, key = await _approved(db)
    check = make_check(agent_id=agent.id, interval_seconds=60)
    db.add(check)
    await db.flush()

    body = await _post(
        client,
        agent,
        key,
        _result(check.id, success=True, at=utc_now() + timedelta(days=3650)),
        _result(check.id, success=True, at=utc_now()),
    )

    assert (body["results_processed"], body["results_failed"]) == (1, 1)


async def test_another_agents_future_success_does_not_hide_a_replicate_checks_outage(
    client: AsyncClient, db: AsyncSession
) -> None:
    (honest, honest_key), (rogue, rogue_key) = await _replicate(db)
    check = await _replicate_check(db, honest)
    await _post(
        client,
        rogue,
        rogue_key,
        _result(check.id, success=True, at=utc_now() + timedelta(days=365)),
    )
    await _failures(client, honest, honest_key, check)

    assert await _health(db, check.id) == CheckHealthStatus.DOWN


async def test_an_agent_clock_a_day_fast_does_not_pin_its_check_up(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, key = await _approved(db)
    check = make_check(agent_id=agent.id, interval_seconds=60)
    db.add(check)
    await db.flush()
    await _post(
        client,
        agent,
        key,
        _result(check.id, success=True, at=utc_now() + timedelta(days=1)),
    )
    await _failures(client, agent, key, check)

    assert await _health(db, check.id) == CheckHealthStatus.DOWN


@pytest.mark.parametrize(
    "ahead",
    [timedelta(days=365), timedelta(minutes=-5)],
    ids=["future-success", "control-past-success"],
)
async def test_a_future_success_does_not_suppress_a_consecutive_failure_alert(
    client: AsyncClient, db: AsyncSession, ahead: timedelta
) -> None:
    (honest, honest_key), (rogue, rogue_key) = await _replicate(db)
    check = await _replicate_check(db, honest)
    await _post(
        client, rogue, rogue_key, _result(check.id, success=True, at=utc_now() + ahead)
    )
    await _failures(client, honest, honest_key, check, n=3)
    alert = make_alert(
        trigger_config={"on_status": ["error"], "consecutive_failures": 3},
    )
    db.add(alert)
    await db.flush()
    newest_failure = next(
        r
        for r in await AlertCRUD.get_recent_results_for_check(db, check.id, 10)
        if not r.success
    )

    assert (
        await AlertEvaluationCoreService._should_fire_alert(
            db, alert, check, newest_failure
        )
        is True
    )


async def test_a_result_within_the_clock_tolerance_is_stored(
    client: AsyncClient, db: AsyncSession
) -> None:
    """An agent clock a few minutes fast (within `RESULT_CLOCK_TOLERANCE`,
    five minutes) is not refused."""
    agent, key = await _approved(db)
    check = make_check(agent_id=agent.id, interval_seconds=60)
    db.add(check)
    await db.flush()

    body = await _post(
        client,
        agent,
        key,
        _result(
            check.id,
            success=True,
            at=utc_now() + timedelta(minutes=4),
        ),
    )

    assert (body["results_processed"], body["results_failed"]) == (1, 0)
