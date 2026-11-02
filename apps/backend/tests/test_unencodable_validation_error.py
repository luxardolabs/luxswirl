"""A refused lone surrogate is a clean 422 that never echoes it, on every surface.

A JSON escape such as `"\\ud800"` decodes to a lone surrogate, text UTF-8
cannot encode. A refusal that quoted it could not be sent: encoding the response
would raise UnicodeEncodeError, a ValueError, and the ValueError handler would
answer 400 in place of the 422. Every text field refuses a surrogate in its own
validator with a message that does not quote it, and the validation handler
drops each error's `input`, so the refusal is a 422 holding only text UTF-8 can
encode: on the admin API, through each branch of the content-negotiated error
renderer (JSON, HTMX toast, HTML page), and as an MCP tool error. A schema a
service validates itself refuses through the ValueError handler (400), and that
refusal holds no surrogate either.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app, validation_exception_handler, value_error_handler
from app.schemas.admin_status_page_schema import AdminStatusPageCreate
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from fixtures.factories import make_agent, make_user
from httpx import ASGITransport, AsyncClient
from mcp_server.test_mcp_wire import _call, _headers, _result, wire_client
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

SURROGATE = "a\ud800"
PAGES = "/api/v1/admin/status-pages"


def _assert_clean(body: bytes) -> None:
    """The body is UTF-8 that names no surrogate, raw or escaped."""
    text = body.decode("utf-8")
    assert "\ud800" not in text
    assert "ud800" not in text.lower()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _token(db: AsyncSession, role: str, scopes: list[str]) -> str:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role, scopes=scopes)
    )
    return plaintext


def _api_headers(token: str, **extra: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        **extra,
    }


# ── Admin API ───────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.parametrize("hx", [False, True])
async def test_a_status_page_description_holding_a_surrogate_is_a_clean_422(
    client: AsyncClient, db: AsyncSession, hx: bool
) -> None:
    extra = {"HX-Request": "true"} if hx else {}
    headers = _api_headers(await _token(db, "editor", ["*"]), **extra)
    body = {"name": "Production", "slug": "surrogate-desc", "description": SURROGATE}

    resp = await client.post(PAGES, content=json.dumps(body).encode(), headers=headers)

    assert resp.status_code == 422, resp.text
    _assert_clean(resp.content)
    payload = resp.json()
    assert payload["error"] == "VALIDATION_ERROR"
    assert [e["loc"] for e in payload["detail"]] == [["body", "description"]]
    assert all("input" not in e for e in payload["detail"])


@pytest.mark.integration
async def test_a_status_page_change_holding_a_surrogate_is_a_clean_422(
    client: AsyncClient, db: AsyncSession
) -> None:
    headers = _api_headers(await _token(db, "editor", ["*"]))
    created = await client.post(
        PAGES,
        content=json.dumps({"name": "Production", "slug": "surrogate-patch"}).encode(),
        headers=headers,
    )
    assert created.status_code == 201, created.text

    resp = await client.patch(
        f"{PAGES}/{created.json()['id']}",
        content=json.dumps({"description": SURROGATE}).encode(),
        headers=headers,
    )

    assert resp.status_code == 422, resp.text
    _assert_clean(resp.content)
    assert resp.json()["error"] == "VALIDATION_ERROR"


# ── The error renderer, each branch ─────────────────────────────────────────


def _request(path: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "scheme": "https",
            "server": (settings.allowed_hosts[0], 443),
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def _surrogate_refusal() -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        AdminStatusPageCreate(name="p", slug="s", description=SURROGATE)
    return caught.value


BRANCHES = [
    pytest.param("/api/v1/admin/status-pages", {}, "application/json", id="json"),
    pytest.param("/status-pages/create", {"HX-Request": "true"}, "", id="htmx"),
    pytest.param("/status-pages/create", {}, "text/html", id="html"),
]


def _check_rendered(response: Response, status: int, media: str) -> None:
    assert response.status_code == status
    _assert_clean(bytes(response.body))
    for value in response.headers.values():
        assert "ud800" not in value.lower()
    if media:
        assert response.headers["content-type"].startswith(media)


@pytest.mark.parametrize(("path", "headers", "media"), BRANCHES)
async def test_the_validation_handler_renders_a_surrogate_refusal_on_every_branch(
    path: str, headers: dict[str, str], media: str
) -> None:
    errors: Any = _surrogate_refusal().errors()
    assert any(e.get("input") == SURROGATE for e in errors), "the error holds it"
    exc = RequestValidationError([{**e, "loc": ("body", *e["loc"])} for e in errors])

    response = await validation_exception_handler(_request(path, headers), exc)

    _check_rendered(response, 422, media)


@pytest.mark.parametrize(("path", "headers", "media"), BRANCHES)
async def test_a_service_s_surrogate_refusal_renders_on_every_branch(
    path: str, headers: dict[str, str], media: str
) -> None:
    """A schema a service validates raises its ValidationError to the ValueError
    handler: its message names the field and reason, never the value."""
    response = await value_error_handler(_request(path, headers), _surrogate_refusal())

    _check_rendered(response, 400, media)


# ── MCP ─────────────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_an_mcp_argument_holding_a_surrogate_is_a_clean_tool_error(
    db: AsyncSession,
) -> None:
    token = await _token(db, "editor", ["*"])
    agent = make_agent()
    db.add(agent)
    await db.flush()
    arguments = {
        "agent_id": str(agent.id),
        "display_name": SURROGATE,
        "check_type": "http",
        "target": "https://api.example.test/health",
    }

    async with wire_client() as mcp:
        response = await mcp.post(
            "/mcp/",
            headers=_headers(token),
            content=json.dumps(_call("create_check", arguments)).encode(),
        )

    assert response.status_code == 200, response.text
    _assert_clean(response.content)
    result = _result(response)
    assert result["isError"] is True
    assert "display_name" in result["content"][0]["text"]
