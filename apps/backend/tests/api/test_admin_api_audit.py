"""Every authenticated admin API call leaves one audit row, and auditing never
fails a call.

The row keeps who called (user and token), the method, the route TEMPLATE (never
the raw path), the status and how long it took. Never the query string or the
body: an id in a path is fine, a secret in a body is not.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.background.job_purge import purge_expired_once
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.models.api_call_model import ApiCall
from app.models.enum_model import HttpMethod
from app.models.mcp_tool_call_model import MCPToolCall
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_audit_core_service import ApiAuditCoreService
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _token(db: AsyncSession, role: str) -> tuple[UUID, UUID, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    token, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=f"audit-{role}", scopes=["*"])
    )
    return user.id, token.id, plaintext


async def _rows(db: AsyncSession, token_id: UUID) -> list[ApiCall]:
    db.expire_all()
    return list(
        (
            await db.scalars(select(ApiCall).where(ApiCall.api_token_id == token_id))
        ).all()
    )


async def test_a_call_writes_one_row_with_the_route_template(
    db: AsyncSession, client: AsyncClient
) -> None:
    user_id, token_id, plaintext = await _token(db, "viewer")
    agent = make_agent()
    db.add(agent)
    await db.flush()
    agent_id = str(agent.id)

    response = await client.get(
        f"/api/v1/admin/agents/{agent_id}?secret=hunter2",
        headers={"Authorization": f"Bearer {plaintext}", "User-Agent": "audit-test"},
    )

    assert response.status_code == 200
    rows = await _rows(db, token_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == user_id
    assert row.method == "GET"
    assert row.route == "/api/v1/admin/agents/{agent_id}"
    assert row.status_code == 200
    assert row.duration_ms is not None and row.duration_ms >= 0
    assert row.user_agent == "audit-test"
    stored = " ".join(str(v) for v in (row.route, row.method, row.user_agent))
    assert "hunter2" not in stored
    assert agent_id not in stored


async def test_a_refused_call_is_recorded_with_its_status(
    db: AsyncSession, client: AsyncClient
) -> None:
    _, token_id, plaintext = await _token(db, "viewer")

    response = await client.post(
        "/api/v1/admin/agents/" + str(uuid4()) + "/pause",
        headers={"Authorization": f"Bearer {plaintext}"},
    )

    assert response.status_code == 403
    rows = await _rows(db, token_id)
    assert [(r.method, r.route, r.status_code) for r in rows] == [
        ("POST", "/api/v1/admin/agents/{agent_id}/pause", 403)
    ]


async def test_an_unauthenticated_call_writes_no_row(
    db: AsyncSession, client: AsyncClient
) -> None:
    db.expire_all()
    before = len((await db.scalars(select(ApiCall))).all())

    response = await client.get(
        "/api/v1/admin/me", headers={"Authorization": "Bearer lsw_nope"}
    )

    assert response.status_code == 401
    db.expire_all()
    assert len((await db.scalars(select(ApiCall))).all()) == before


async def test_a_failed_audit_write_never_fails_the_call(
    db: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token_id, plaintext = await _token(db, "viewer")

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit table is gone")

    monkeypatch.setattr(ApiAuditCoreService, "log_call", broken)

    response = await client.get(
        "/api/v1/admin/me", headers={"Authorization": f"Bearer {plaintext}"}
    )

    assert response.status_code == 200
    assert response.json()["token_id"] == str(token_id)
    assert await _rows(db, token_id) == []


async def test_rows_past_ninety_days_are_purged_with_the_mcp_rows(
    db: AsyncSession,
) -> None:
    user_id, token_id, _ = await _token(db, "viewer")
    old = utc_now() - timedelta(days=91)
    recent = utc_now() - timedelta(days=89)
    for at in (old, recent):
        db.add(
            ApiCall(
                user_id=user_id,
                api_token_id=token_id,
                method=HttpMethod.GET,
                route="/api/v1/admin/me",
                status_code=200,
                duration_ms=1,
                created_at=at,
            )
        )
        db.add(
            MCPToolCall(
                user_id=user_id,
                api_token_id=token_id,
                tool_name="whoami",
                arguments={},
                success=True,
                created_at=at,
            )
        )
    await db.flush()

    await purge_expired_once(db)

    db.expire_all()
    api_left = (
        await db.scalars(
            select(ApiCall.created_at).where(ApiCall.api_token_id == token_id)
        )
    ).all()
    mcp_left = (
        await db.scalars(
            select(MCPToolCall.created_at).where(MCPToolCall.api_token_id == token_id)
        )
    ).all()
    assert [abs((at - recent).total_seconds()) < 1 for at in api_left] == [True]
    assert [abs((at - recent).total_seconds()) < 1 for at in mcp_left] == [True]
