"""The alert API reports how many providers and checks each alert is wired to.

List, get and update all answered `notification_provider_count: 0` and
`check_count: 0` (a TODO), whatever was mapped.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.db.database import get_db
from app.main import app
from fixtures.factories import (
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_alert_notification_mapping,
    make_check,
    make_notification_provider,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", False)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def wired_alert_id(db: AsyncSession) -> str:
    agent = make_agent()
    alert = make_alert()
    providers = [make_notification_provider() for _ in range(2)]
    db.add_all([agent, alert, *providers])
    await db.flush()
    checks = [make_check(agent_id=agent.id) for _ in range(3)]
    db.add_all(checks)
    await db.flush()
    db.add_all(
        [
            *(
                make_alert_notification_mapping(
                    alert_id=alert.id, notification_provider_id=p.id
                )
                for p in providers
            ),
            *(
                make_alert_check_mapping(alert_id=alert.id, check_id=c.id)
                for c in checks
            ),
        ]
    )
    await db.flush()
    alert_id = str(alert.id)
    db.expunge_all()
    return alert_id


def _counts(body: dict) -> tuple[int, int]:
    return body["notification_provider_count"], body["check_count"]


async def test_get_reports_the_counts(client: AsyncClient, wired_alert_id: str) -> None:
    resp = await client.get(f"/api/v1/alerts/{wired_alert_id}")

    assert resp.status_code == 200, resp.text
    assert _counts(resp.json()) == (2, 3)


async def test_list_reports_the_counts(
    client: AsyncClient, wired_alert_id: str
) -> None:
    resp = await client.get("/api/v1/alerts", params={"per_page": 1000})

    assert resp.status_code == 200, resp.text
    [row] = [a for a in resp.json()["items"] if a["id"] == wired_alert_id]
    assert _counts(row) == (2, 3)


async def test_update_reports_the_counts(
    client: AsyncClient, wired_alert_id: str
) -> None:
    resp = await client.patch(
        f"/api/v1/alerts/{wired_alert_id}", json={"description": "renamed"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "renamed"
    assert _counts(resp.json()) == (2, 3)


async def test_create_reports_the_counts(client: AsyncClient, db: AsyncSession) -> None:
    agent = make_agent()
    provider = make_notification_provider()
    db.add_all([agent, provider])
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()

    resp = await client.post(
        "/api/v1/alerts",
        json={
            "name": f"created-{uuid4().hex[:6]}",
            "trigger_type": "status_change",
            "trigger_config": {},
            "notification_provider_ids": [str(provider.id)],
            "check_ids": [str(check.id)],
        },
    )

    assert resp.status_code == 201, resp.text
    assert _counts(resp.json()) == (1, 1)
