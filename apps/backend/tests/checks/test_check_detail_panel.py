"""The check detail panel renders from DTOs: the check, its recent results and,
for a synthetic check, its latest result's artifacts (metadata only)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.main import app
from app.models.check_artifact_model import CheckArtifact
from app.models.enum_model import CheckArtifactType
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

# Exactly what the agent's synthetic check reports when the user's script
# returns only a status (apps/agent/app/checks/synthetic.py): no track_* keys.
AGENT_SYNTHETIC_RESULT = {
    "status": "success",
    "steps": ["opened login", "submitted form"],
    "errors": [],
    "console_errors": [],
    "request_failures": [],
    "browser_timing": {},
    "total_execution_time_ms": 1834.2,
}


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


async def test_detail_panel_with_recent_results_and_an_artifact(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(agent_name="edge")
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        display_name="login-flow",
        check_type="synthetic",
        target="https://app.example.com/login",
    )
    db.add(check)
    await db.flush()
    results = [
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            success=minutes != 1,
            timestamp=utc_now() - timedelta(minutes=minutes, seconds=5),
            metrics=json.dumps({"synthetic": AGENT_SYNTHETIC_RESULT}),
        )
        for minutes in range(3)
    ]
    db.add_all(results)
    await db.flush()
    latest = results[0]
    db.add(
        CheckArtifact(
            id=uuid4(),
            check_id=check.id,
            check_result_id=latest.id,
            check_result_timestamp=latest.timestamp,
            artifact_type=CheckArtifactType.SCREENSHOT,
            content_type="image/png",
            filename="final-state.png",
            size_bytes=4,
            data=b"\x89PNG",
            created_at=latest.timestamp,
            updated_at=latest.timestamp,
        )
    )
    await db.flush()

    resp = await client.get(f"/check/{check.id}")

    assert resp.status_code == 200, resp.text[:500]
    assert "login-flow" in resp.text
    assert "final-state.png" in resp.text
