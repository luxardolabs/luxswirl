"""POST /api/v1/reports validates each result on its own.

A report carries up to thousands of results. One the server cannot store (an
HTTP status outside 100-599, an error text over the limit) is counted in
`results_failed` and skipped; the rest of the report is stored. Only the
envelope (the agent id, the item count, a non-object item) refuses the whole
report with 422, since the agent dead-letters a refused report.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.models.check_result_model import CheckResult
from app.services.core.agent_core_service import AgentCoreService
from fixtures.factories import make_agent, make_check
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
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


@pytest_asyncio.fixture
async def approved(db: AsyncSession) -> tuple[UUID, str]:
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id)
    return agent.id, key


def _wire_result(check_id: UUID, **kw: Any) -> dict[str, Any]:
    return {
        "check_id": str(check_id),
        "display_name": "probe",
        "check_type": "http",
        "target": "https://example.com",
        "timestamp": utc_now().isoformat(),
        "success": True,
        "latency_ms": 5.0,
        **kw,
    }


async def _stored(db: AsyncSession, check_id: UUID) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(CheckResult)
        .where(CheckResult.check_id == check_id)
    )
    return int(count or 0)


@pytest.mark.parametrize(
    "bad",
    [
        {"http_status_code": 999},
        {"success": False, "error": "x" * 4001},
    ],
    ids=["status-999", "long-error"],
)
async def test_one_odd_result_does_not_refuse_the_whole_report(
    client: AsyncClient, db: AsyncSession, approved: tuple[UUID, str], bad: dict
) -> None:
    agent_id, key = approved
    good_check = make_check(agent_id=agent_id, check_type="http", target="https://a")
    odd_check = make_check(agent_id=agent_id, check_type="http", target="https://b")
    db.add_all([good_check, odd_check])
    await db.flush()
    headers = {"Authorization": f"Bearer {key}"}

    control = await client.post(
        "/api/v1/reports",
        json={"agent_id": str(agent_id), "checks": [_wire_result(good_check.id)]},
        headers=headers,
    )
    assert control.status_code == 202, control.text

    resp = await client.post(
        "/api/v1/reports",
        json={
            "agent_id": str(agent_id),
            "checks": [
                _wire_result(good_check.id),
                _wire_result(odd_check.id, **bad),
            ],
        },
        headers=headers,
    )

    assert resp.status_code == 202, (
        f"whole report refused {resp.status_code} for one result: the agent "
        "dead-letters every result in the batch"
    )
    assert resp.json()["results_processed"] == 1
    assert resp.json()["results_failed"] == 1
    assert await _stored(db, good_check.id) == 2
    assert await _stored(db, odd_check.id) == 0


@pytest.mark.parametrize(
    "envelope",
    [
        {"agent_id": "not-a-uuid", "checks": []},
        {"checks": []},
        {"checks": ["not-an-object"]},
    ],
    ids=["bad-agent-id", "no-agent-id", "non-object-item"],
)
async def test_a_garbage_envelope_is_still_refused(
    client: AsyncClient, approved: tuple[UUID, str], envelope: dict
) -> None:
    agent_id, key = approved
    body = {"agent_id": str(agent_id), **envelope}
    if "agent_id" not in envelope:
        body.pop("agent_id")

    resp = await client.post(
        "/api/v1/reports", json=body, headers={"Authorization": f"Bearer {key}"}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "VALIDATION_ERROR"


async def test_more_results_than_the_cap_are_refused(
    client: AsyncClient, approved: tuple[UUID, str]
) -> None:
    agent_id, key = approved

    resp = await client.post(
        "/api/v1/reports",
        json={"agent_id": str(agent_id), "checks": [{}] * 10001},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 422, resp.text
