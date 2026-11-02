"""Adversarial probes against the admin API call audit and the API activity pages."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import anyio
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.api_call_model import ApiCall
from app.models.enum_model import HttpMethod
from app.models.mcp_tool_call_model import MCPToolCall
from app.models.user_model import User
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_audit_core_service import ApiAuditCoreService
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.mcp_audit_core_service import (
    USER_AGENT_LENGTH,
    MCPAuditCoreService,
    masked_arguments,
)
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message

pytestmark = pytest.mark.integration

PAGE = "/settings/api-activity"
XSS = '<script>alert("pwn")</script>'


@pytest_asyncio.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _web(db: AsyncSession, user: User) -> AsyncIterator[AsyncClient]:
    _, session_token = await AuthCoreService().create_session(db, user.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, session_token)
        yield c


async def _user_token(db: AsyncSession, role: str) -> tuple[User, UUID, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    token, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=f"atk-{role}", scopes=["*"])
    )
    return user, token.id, plaintext


async def _rows(db: AsyncSession, token_id: UUID) -> list[ApiCall]:
    db.expire_all()
    return list(
        (
            await db.scalars(select(ApiCall).where(ApiCall.api_token_id == token_id))
        ).all()
    )


# ── Integrity: the MCP call detail re-masks what was already masked ───────────


async def test_detail_panel_keeps_the_real_script_size(db: AsyncSession) -> None:
    """A synthetic script is stored as `<5000 characters>`; the detail panel
    masks the stored arguments again, so it must still say 5000."""
    user, token_id, _ = await _user_token(db, "admin")
    row = MCPToolCall(
        user_id=user.id,
        api_token_id=token_id,
        tool_name="create_check",
        # Exactly what MCPAuditCoreService.log_call stores.
        arguments=masked_arguments({"script_code": "x" * 5000, "name": "s"}),
        success=True,
    )
    db.add(row)
    await db.flush()
    assert row.arguments["script_code"] == "<5000 characters>"

    async for client in _web(db, user):
        response = await client.get(f"{PAGE}/mcp/{row.id}")
    assert response.status_code == 200, response.text[:400]
    assert "17 characters" not in response.text, "shows the placeholder's length"
    assert "5000 characters" in response.text


async def test_control_detail_panel_masks_a_credential(db: AsyncSession) -> None:
    user, token_id, _ = await _user_token(db, "admin")
    row = MCPToolCall(
        user_id=user.id,
        api_token_id=token_id,
        tool_name="create_check",
        arguments=masked_arguments({"target": "mysql://u:pw123456@h/db"}),
        success=True,
    )
    db.add(row)
    await db.flush()
    async for client in _web(db, user):
        response = await client.get(f"{PAGE}/mcp/{row.id}")
    assert response.status_code == 200
    assert "pw123456" not in response.text


# ── Stored XSS through attacker-controlled audit fields ───────────────────────


async def test_crafted_user_agent_and_tool_name_render_escaped(
    db: AsyncSession, api_client: AsyncClient
) -> None:
    attacker, token_id, plaintext = await _user_token(db, "viewer")
    attacker_id = attacker.id
    response = await api_client.get(
        "/api/v1/admin/me",
        headers={"Authorization": f"Bearer {plaintext}", "User-Agent": XSS},
    )
    assert response.status_code == 200
    assert [r.user_agent for r in await _rows(db, token_id)] == [XSS]
    db.add(
        MCPToolCall(
            user_id=attacker_id,
            api_token_id=token_id,
            tool_name=XSS,
            arguments={XSS: XSS},
            success=False,
            error_message=XSS,
            user_agent=XSS,
        )
    )
    await db.flush()
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    async for client in _web(db, admin):
        api_page = await client.get(f"{PAGE}?surface=api")
        mcp_page = await client.get(f"{PAGE}?surface=mcp")
        own = None
    await db.refresh(attacker)  # expired by _rows; reload before reading .id
    async for client in _web(db, attacker):
        own = await client.get("/profile/activity?surface=mcp")
    for page in (api_page, mcp_page, own):
        assert page is not None and page.status_code == 200
        assert XSS not in page.text


# ── Integrity: an unhandled exception after authentication ────────────────────


async def test_an_unhandled_error_is_recorded_as_500(
    db: AsyncSession, api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token_id, plaintext = await _user_token(db, "viewer")

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ApiTokenCoreService, "who_am_i", boom)
    response = await api_client.get(
        "/api/v1/admin/me", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert response.status_code == 500
    rows = await _rows(db, token_id)
    assert [(r.route, r.status_code) for r in rows] == [("/api/v1/admin/me", 500)]


async def test_a_validation_error_is_recorded_with_its_template(
    db: AsyncSession, api_client: AsyncClient
) -> None:
    _, token_id, plaintext = await _user_token(db, "viewer")
    response = await api_client.get(
        "/api/v1/admin/agents/not-a-uuid",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    rows = await _rows(db, token_id)
    assert [(r.method, r.route, r.status_code) for r in rows] == [
        (HttpMethod.GET, "/api/v1/admin/agents/{agent_id}", response.status_code)
    ]
    assert "not-a-uuid" not in rows[0].route


async def test_the_openapi_route_is_audited_by_template(
    db: AsyncSession, api_client: AsyncClient
) -> None:
    _, token_id, plaintext = await _user_token(db, "viewer")
    response = await api_client.get(
        "/api/v1/admin/openapi.json",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 200
    rows = await _rows(db, token_id)
    assert [(r.route, r.status_code) for r in rows] == [
        ("/api/v1/admin/openapi.json", 200)
    ]


# ── Access: the profile view never shows another user's rows ──────────────────


async def test_profile_ignores_a_forged_user_filter(db: AsyncSession) -> None:
    alice, alice_token, _ = await _user_token(db, "viewer")
    bob, bob_token, _ = await _user_token(db, "editor")
    db.add(
        ApiCall(
            user_id=bob.id,
            api_token_id=bob_token,
            method=HttpMethod.GET,
            route="/api/v1/admin/bob-secret-route",
            status_code=200,
        )
    )
    await db.flush()
    async for client in _web(db, alice):
        r = await client.get(
            f"/profile/activity?surface=api&user_id={bob.id}&api_token_id={bob_token}"
        )
        detail = await client.get(f"{PAGE}?surface=api&user_id={bob.id}")
    assert r.status_code == 200
    assert "bob-secret-route" not in r.text
    assert detail.status_code in (303, 307, 403)


# ── Integrity: a client that disconnects mid-response still leaves its row ───


async def test_a_call_cancelled_during_its_audit_write_is_still_recorded(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that disconnects makes the outer BaseHTTPMiddleware cancel its task
    group, with the audit write in flight; the write still completes."""
    _, token_id, plaintext = await _user_token(db, "viewer")
    write_started = anyio.Event()
    log_call = ApiAuditCoreService.log_call

    async def slow_log_call(*args: Any, **kwargs: Any) -> None:
        write_started.set()
        await anyio.sleep(0.2)
        await log_call(*args, **kwargs)

    monkeypatch.setattr(ApiAuditCoreService, "log_call", slow_log_call)
    host = settings.allowed_hosts[0]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/v1/admin/me",
        "raw_path": b"/api/v1/admin/me",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", host.encode()),
            (b"authorization", f"Bearer {plaintext}".encode()),
        ],
        "client": ("203.0.113.9", 4000),
        "server": (host, 443),
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    limiter.reset()
    async with anyio.create_task_group() as tg:
        tg.start_soon(app, scope, receive, send)
        with anyio.fail_after(10):
            await write_started.wait()
        tg.cancel_scope.cancel()
    limiter.reset()

    assert sent and sent[0]["type"] == "http.response.start"
    rows = await _rows(db, token_id)
    assert [(r.route, r.status_code) for r in rows] == [("/api/v1/admin/me", 200)]


# ── Integrity: an MCP row keeps the user agent cut as an API row does ─────────


async def test_mcp_user_agent_is_cut_like_the_api_rows(db: AsyncSession) -> None:
    await MCPAuditCoreService.log_call(
        db,
        principal=None,
        tool_name="long-ua-probe",
        arguments={},
        success=True,
        error_message=None,
        duration_ms=1,
        denial_code=None,
        ip_address=None,
        user_agent="A" * 10_000,
    )
    await db.flush()
    row = await db.scalar(
        select(MCPToolCall).where(MCPToolCall.tool_name == "long-ua-probe")
    )
    assert row is not None
    assert row.user_agent == "A" * USER_AGENT_LENGTH
