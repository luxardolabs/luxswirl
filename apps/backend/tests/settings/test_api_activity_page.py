"""API activity: the admin page over every user's MCP and admin API calls, and
each user's own calls in My Account.

Rows are matched by their `data-call-id` marker, never by a name: the filter
dropdowns list every tool, token and user with traffic, so a name on the page
does not mean its row is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.models.api_call_model import ApiCall
from app.models.enum_model import HttpMethod, MCPDenialCode
from app.models.mcp_tool_call_model import MCPToolCall
from app.models.user_model import User
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PAGE = "/settings/api-activity"
OWN = "/profile/activity"


async def _client(db: AsyncSession, user: User) -> AsyncIterator[AsyncClient]:
    _, session_token = await AuthCoreService().create_session(db, user.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, session_token)
        yield c


async def _user(db: AsyncSession, role: str) -> User:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    return user


async def _mcp(db: AsyncSession, user: User, token_id: UUID, **kw: Any) -> UUID:
    row = MCPToolCall(
        user_id=user.id,
        api_token_id=token_id,
        tool_name=kw.pop("tool_name", "list_checks"),
        arguments=kw.pop("arguments", {}),
        success=kw.pop("success", True),
        duration_ms=12,
        ip_address="10.1.2.3",
        **kw,
    )
    db.add(row)
    await db.flush()
    return row.id


async def _api(db: AsyncSession, user: User, token_id: UUID, **kw: Any) -> UUID:
    row = ApiCall(
        user_id=user.id,
        api_token_id=token_id,
        method=HttpMethod(kw.pop("method", "GET")),
        route=kw.pop("route", "/api/v1/admin/checks"),
        status_code=kw.pop("status_code", 200),
        duration_ms=7,
        ip_address="10.9.8.7",
    )
    db.add(row)
    await db.flush()
    return row.id


def _marker(call_id: UUID) -> str:
    return f'data-call-id="{call_id}"'


@pytest_asyncio.fixture
async def seeded(db: AsyncSession) -> dict[str, Any]:
    """Two users with a token each, and calls of every outcome on both surfaces."""
    bob = await _user(db, "editor")
    carol = await _user(db, "viewer")
    bob_token, _ = await ApiTokenCoreService.create_token(
        db, bob.id, ApiTokenCreate(name="bob-laptop", scopes=["*"])
    )
    carol_token, _ = await ApiTokenCoreService.create_token(
        db, carol.id, ApiTokenCreate(name="carol-ci", scopes=["*"])
    )
    return {
        "bob": bob,
        "carol": carol,
        "bob_token": bob_token.id,
        "carol_token": carol_token.id,
        "bob_ok": await _mcp(
            db,
            bob,
            bob_token.id,
            tool_name="create_check",
            arguments={"target": "postgresql://mon:s3cretpw@db:5432/x"},
        ),
        "bob_error": await _mcp(
            db, bob, bob_token.id, success=False, error_message="no such check"
        ),
        "carol_denied": await _mcp(
            db,
            carol,
            carol_token.id,
            tool_name="update_check",
            success=False,
            error_message="role_insufficient: editor required",
            denial_code=MCPDenialCode.ROLE_INSUFFICIENT,
        ),
        "bob_api": await _api(db, bob, bob_token.id),
        "carol_api_denied": await _api(
            db,
            carol,
            carol_token.id,
            method="POST",
            route="/api/v1/admin/agents/{agent_id}/pause",
            status_code=403,
        ),
    }


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_a_non_admin_is_turned_away(db: AsyncSession, role: str) -> None:
    user = await _user(db, role)
    async for client in _client(db, user):
        for path in (PAGE, f"{PAGE}?surface=api"):
            response = await client.get(path)
            assert response.status_code == 307, (path, response.status_code)
            assert "admin_required" in response.headers["location"]


async def test_an_admin_sees_every_users_mcp_calls(
    db: AsyncSession, seeded: dict[str, Any]
) -> None:
    admin = await _user(db, "admin")
    async for client in _client(db, admin):
        response = await client.get(PAGE)
    assert response.status_code == 200, response.text[:400]
    for key in ("bob_ok", "bob_error", "carol_denied"):
        assert _marker(seeded[key]) in response.text, key
    assert "role_insufficient" in response.text
    assert "10.1.2.3" in response.text
    assert "bob-laptop" in response.text
    assert seeded["bob"].username in response.text


@pytest.mark.parametrize(
    ("params", "shown", "hidden"),
    [
        ({"outcome": "success"}, ["bob_ok"], ["bob_error", "carol_denied"]),
        ({"outcome": "error"}, ["bob_error"], ["bob_ok", "carol_denied"]),
        ({"outcome": "denied"}, ["carol_denied"], ["bob_ok", "bob_error"]),
        ({"tool": "create_check"}, ["bob_ok"], ["bob_error", "carol_denied"]),
        ({"user_id": "carol"}, ["carol_denied"], ["bob_ok", "bob_error"]),
        ({"api_token_id": "bob_token"}, ["bob_ok", "bob_error"], ["carol_denied"]),
        ({"days": "3650"}, ["bob_ok", "bob_error", "carol_denied"], []),
    ],
)
async def test_the_mcp_filters_narrow_the_rows(
    db: AsyncSession,
    seeded: dict[str, Any],
    params: dict[str, str],
    shown: list[str],
    hidden: list[str],
) -> None:
    query = dict(params)
    if query.get("user_id") == "carol":
        query["user_id"] = str(seeded["carol"].id)
    if query.get("api_token_id") == "bob_token":
        query["api_token_id"] = str(seeded["bob_token"])
    admin = await _user(db, "admin")
    async for client in _client(db, admin):
        response = await client.get(PAGE, params=query)
    assert response.status_code == 200, response.text[:400]
    for key in shown:
        assert _marker(seeded[key]) in response.text, (params, key)
    for key in hidden:
        assert _marker(seeded[key]) not in response.text, (params, key)


async def test_an_admin_sees_every_users_api_calls_and_their_filters(
    db: AsyncSession, seeded: dict[str, Any]
) -> None:
    admin = await _user(db, "admin")
    async for client in _client(db, admin):
        everything = await client.get(PAGE, params={"surface": "api"})
        denied = await client.get(PAGE, params={"surface": "api", "outcome": "denied"})
        bob_only = await client.get(
            PAGE, params={"surface": "api", "user_id": str(seeded["bob"].id)}
        )
    assert everything.status_code == 200, everything.text[:400]
    assert _marker(seeded["bob_api"]) in everything.text
    assert _marker(seeded["carol_api_denied"]) in everything.text
    assert "/api/v1/admin/agents/{agent_id}/pause" in everything.text
    assert _marker(seeded["carol_api_denied"]) in denied.text
    assert _marker(seeded["bob_api"]) not in denied.text
    assert _marker(seeded["bob_api"]) in bob_only.text
    assert _marker(seeded["carol_api_denied"]) not in bob_only.text


async def test_the_call_detail_shows_the_masked_arguments(
    db: AsyncSession, seeded: dict[str, Any]
) -> None:
    admin = await _user(db, "admin")
    async for client in _client(db, admin):
        response = await client.get(f"{PAGE}/mcp/{seeded['bob_ok']}")
    assert response.status_code == 200, response.text[:400]
    assert "create_check" in response.text
    assert "postgresql://***@db:5432/x" in response.text
    assert "s3cretpw" not in response.text


async def test_a_non_admin_cannot_open_a_call(
    db: AsyncSession, seeded: dict[str, Any]
) -> None:
    async for client in _client(db, seeded["carol"]):
        response = await client.get(f"{PAGE}/mcp/{seeded['carol_denied']}")
    assert response.status_code == 307


@pytest.mark.parametrize("surface", ["mcp", "api"])
async def test_my_account_shows_only_my_own_calls(
    db: AsyncSession, seeded: dict[str, Any], surface: str
) -> None:
    async for client in _client(db, seeded["bob"]):
        response = await client.get(OWN, params={"surface": surface})
    assert response.status_code == 200, response.text[:400]
    if surface == "mcp":
        assert _marker(seeded["bob_ok"]) in response.text
        assert _marker(seeded["bob_error"]) in response.text
        assert _marker(seeded["carol_denied"]) not in response.text
    else:
        assert _marker(seeded["bob_api"]) in response.text
        assert _marker(seeded["carol_api_denied"]) not in response.text
    assert "carol-ci" not in response.text
    assert seeded["carol"].username not in response.text


async def test_my_account_panel_offers_the_activity(
    db: AsyncSession, seeded: dict[str, Any]
) -> None:
    async for client in _client(db, seeded["bob"]):
        response = await client.get("/profile")
    assert response.status_code == 200
    assert OWN in response.text
