"""A check read through the admin API or MCP carries its health, as the dashboard shows it.

`latest_status` is up or down from the check's newest result within its silence
threshold, and unknown when there is none (it never reported, or has gone
silent). `latest_latency_ms` is that result's latency. `success_rate_24h` is the
share of the last 24 hours' results that succeeded, null with none. The list
filters on the same health.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from app.core import rate_limit
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.mcp.server import MCPAuthMiddleware, mcp_server
from app.models.enum_model import CheckHealthStatus, UserRole
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.admin_check_core_service import AdminCheckCoreService
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.views.status_view_service import StatusViewService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.routing import Mount

pytestmark = pytest.mark.integration

CHECKS = "/api/v1/admin/checks"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _token(db: AsyncSession) -> str:
    user = make_user(role="viewer")
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="health", scopes=["*"])
    )
    return plaintext


async def _world(db: AsyncSession) -> dict[str, Any]:
    """One agent's checks in each state: up, down, gone silent, never reported."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    now = utc_now()
    checks = {}
    for name in ("up", "down", "silent", "never"):
        check = make_check(
            agent_id=agent.id, display_name=f"health-{name}", interval_seconds=60
        )
        db.add(check)
        checks[name] = check
    await db.flush()

    def result(name: str, minutes_ago: float, success: bool, latency: float) -> None:
        db.add(
            make_check_result(
                check_id=checks[name].id,
                agent_id=agent.id,
                timestamp=now - timedelta(minutes=minutes_ago),
                success=success,
                latency_ms=latency,
            )
        )

    # Up: newest succeeded at 20 ms; 3 of the day's 4 succeeded.
    result("up", 0.5, True, 20.0)
    result("up", 60, True, 30.0)
    result("up", 120, False, 40.0)
    result("up", 180, True, 50.0)
    # Down: newest failed; an older success that day.
    result("down", 0.5, False, 900.0)
    result("down", 90, True, 15.0)
    # Gone silent: its only result is hours older than its threshold.
    result("silent", 300, True, 10.0)
    # Out of the 24 h window: counted nowhere.
    result("never", 60 * 25, True, 10.0)
    await db.flush()
    return {"agent": agent, "checks": checks}


def _by_name(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["display_name"].removeprefix("health-"): item for item in items}


async def test_the_list_carries_each_check_s_health(
    db: AsyncSession, client: AsyncClient
) -> None:
    world = await _world(db)
    headers = {"Authorization": f"Bearer {await _token(db)}"}

    response = await client.get(
        CHECKS, params={"agent_id": str(world["agent"].id)}, headers=headers
    )

    assert response.status_code == 200, response.text
    rows = _by_name(response.json()["items"])
    assert rows["up"]["latest_status"] == "up"
    assert rows["up"]["latest_latency_ms"] == 20.0
    assert rows["up"]["success_rate_24h"] == 75.0
    assert rows["down"]["latest_status"] == "down"
    assert rows["down"]["latest_latency_ms"] == 900.0
    assert rows["down"]["success_rate_24h"] == 50.0
    assert rows["silent"]["latest_status"] == "unknown"
    assert rows["silent"]["latest_latency_ms"] is None
    assert rows["silent"]["success_rate_24h"] == 100.0
    assert rows["never"]["latest_status"] == "unknown"
    assert rows["never"]["latest_latency_ms"] is None
    assert rows["never"]["success_rate_24h"] is None


async def test_one_check_carries_its_health(
    db: AsyncSession, client: AsyncClient
) -> None:
    world = await _world(db)
    headers = {"Authorization": f"Bearer {await _token(db)}"}

    response = await client.get(
        f"{CHECKS}/{world['checks']['down'].id}", headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["latest_status"] == "down"
    assert body["latest_latency_ms"] == 900.0
    assert body["success_rate_24h"] == 50.0


@pytest.mark.parametrize(
    ("status", "expected"),
    [("up", {"up"}), ("down", {"down"}), ("unknown", {"silent", "never"})],
)
async def test_the_list_filters_on_health(
    db: AsyncSession, client: AsyncClient, status: str, expected: set[str]
) -> None:
    world = await _world(db)
    headers = {"Authorization": f"Bearer {await _token(db)}"}

    response = await client.get(
        CHECKS,
        params={"agent_id": str(world["agent"].id), "status": status},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(_by_name(body["items"])) == expected
    assert body["total"] == len(expected)


async def test_a_status_outside_the_set_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    headers = {"Authorization": f"Bearer {await _token(db)}"}

    response = await client.get(CHECKS, params={"status": "broken"}, headers=headers)

    assert response.status_code == 422


@asynccontextmanager
async def _mcp_client() -> AsyncIterator[httpx.AsyncClient]:
    rate_limit.credential_attempts.reset()
    rate_limit.mcp_token_calls.reset()
    manager = StreamableHTTPSessionManager(
        app=mcp_server, json_response=True, stateless=True
    )
    mcp_app = Starlette(
        routes=[Mount("/mcp", app=MCPAuthMiddleware(StreamableHTTPASGIApp(manager)))]
    )
    async with (
        manager.run(),
        httpx.AsyncClient(
            transport=ASGITransport(app=mcp_app), base_url="http://testserver"
        ) as c,
    ):
        yield c


async def test_mcp_list_checks_filters_on_health_and_carries_it(
    db: AsyncSession,
) -> None:
    world = await _world(db)
    token = await _token(db)

    async with _mcp_client() as c:
        response = await c.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "list_checks",
                    "arguments": {
                        "agent_id": str(world["agent"].id),
                        "status": "down",
                    },
                },
            },
        )

    result = response.json()["result"]
    assert result["isError"] is False, result
    rows = _by_name(json.loads(result["content"][0]["text"])["items"])
    assert set(rows) == {"down"}
    assert rows["down"]["latest_status"] == "down"
    assert rows["down"]["success_rate_24h"] == 50.0


async def test_a_result_landing_after_the_filter_does_not_change_the_health_served(
    db: AsyncSession, monkeypatch
) -> None:
    """The statement that filters on status also reads the health served, so a
    report arriving mid-request cannot list a check as down and serve it as up."""
    world = await _world(db)
    down = world["checks"]["down"]
    listed = CheckCoreService.list_checks_with_health

    async def then_a_report_lands(*args: Any, **kwargs: Any) -> Any:
        page = await listed(*args, **kwargs)
        db.add(
            make_check_result(
                check_id=down.id, agent_id=world["agent"].id, success=True
            )
        )
        await db.flush()
        return page

    monkeypatch.setattr(
        CheckCoreService, "list_checks_with_health", then_a_report_lands
    )

    page = await AdminCheckCoreService.list_checks(
        db,
        UserRole.VIEWER,
        page=1,
        per_page=50,
        agent_id=world["agent"].id,
        status=CheckHealthStatus.DOWN,
    )

    assert page.total == 1
    assert [c.latest_status for c in page.items] == [CheckHealthStatus.DOWN]


async def test_an_interval_too_large_for_int4_arithmetic_still_reads(
    db: AsyncSession, client: AsyncClient
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id, display_name="huge", interval_seconds=800_000_000
    )
    db.add(check)
    await db.flush()
    db.add(make_check_result(check_id=check.id, agent_id=agent.id))
    await db.flush()
    headers = {"Authorization": f"Bearer {await _token(db)}"}

    response = await client.get(f"{CHECKS}/{check.id}", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["latest_status"] == "up"


async def test_an_interval_of_zero_reads_as_the_default_as_the_dashboard_reads_it(
    db: AsyncSession, client: AsyncClient
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, display_name="zero", interval_seconds=0)
    db.add(check)
    await db.flush()
    # Inside the default threshold (240 s), outside a 0 s interval's (60 s).
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            timestamp=utc_now() - timedelta(seconds=150),
        )
    )
    await db.flush()
    headers = {"Authorization": f"Bearer {await _token(db)}"}

    response = await client.get(f"{CHECKS}/{check.id}", headers=headers)
    rows, _ = await StatusViewService.get_all_checks_status(db, check_ids=[check.id])

    assert response.json()["latest_status"] == "up"
    assert rows[0].health == CheckHealthStatus.UP


async def test_a_write_returns_the_check_with_its_health(
    db: AsyncSession, client: AsyncClient
) -> None:
    world = await _world(db)
    editor = make_user(role="editor")
    db.add(editor)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, editor.id, ApiTokenCreate(name="editor", scopes=["*"])
    )

    response = await client.post(
        f"{CHECKS}/{world['checks']['down'].id}/disable",
        headers={"Authorization": f"Bearer {plaintext}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["latest_status"] == "down"
    assert response.json()["success_rate_24h"] == 50.0


async def test_a_viewer_reads_health_and_still_no_credential(
    db: AsyncSession, client: AsyncClient
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        check_type="postgres",
        target="db.internal",
        connection_string="postgresql://monitor:s3cret-pw@db.internal:5432/app",
    )
    db.add(check)
    await db.flush()
    db.add(make_check_result(check_id=check.id, agent_id=agent.id))
    await db.flush()
    headers = {"Authorization": f"Bearer {await _token(db)}"}

    response = await client.get(
        CHECKS, params={"agent_id": str(agent.id), "status": "up"}, headers=headers
    )

    (row,) = response.json()["items"]
    assert row["latest_status"] == "up"
    assert "s3cret-pw" not in response.text
