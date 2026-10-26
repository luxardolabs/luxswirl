"""Checks on the admin API: addressed by id, paged, written only by editors.

A viewer reads checks without the secrets the web UI's edit form holds:
the database password and the synthetic script. Writes keep every rule the
web UI keeps (SSRF, synthetic is admin-only), because both go through the
same core service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

CHECKS = "/api/v1/admin/checks"
DSN = "postgresql://monitor:s3cret-pw@db.internal:5432/app"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _as(db: AsyncSession, role: str) -> dict[str, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role)
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _agent(db: AsyncSession):
    agent = make_agent()
    db.add(agent)
    await db.flush()
    return agent


async def _check(db: AsyncSession, agent, **overrides):
    check = make_check(agent_id=agent.id, **overrides)
    db.add(check)
    await db.flush()
    return check


async def test_the_list_is_one_page_in_the_envelope(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    agent = await _agent(db)
    for n in range(3):
        await _check(db, agent, display_name=f"page-{n}")

    first = await client.get(
        CHECKS, params={"agent_id": str(agent.id), "per_page": 2}, headers=viewer
    )

    assert first.status_code == 200
    body = first.json()
    assert (body["total"], body["page"], body["per_page"]) == (3, 1, 2)
    assert (body["total_pages"], body["has_next"]) == (2, True)
    assert len(body["items"]) == 2
    second = await client.get(
        CHECKS,
        params={"agent_id": str(agent.id), "per_page": 2, "page": 2},
        headers=viewer,
    )
    assert second.json()["has_next"] is False
    names = {c["display_name"] for c in body["items"] + second.json()["items"]}
    assert names == {"page-0", "page-1", "page-2"}


async def test_the_list_filters_by_type_and_search(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    agent = await _agent(db)
    await _check(
        db,
        agent,
        display_name="web-front",
        check_type="http",
        target="https://a.example",
    )
    await _check(db, agent, display_name="db-ping", check_type="ping")

    by_type = await client.get(
        CHECKS, params={"agent_id": str(agent.id), "check_type": "http"}, headers=viewer
    )
    by_search = await client.get(
        CHECKS, params={"agent_id": str(agent.id), "search": "db-"}, headers=viewer
    )

    assert [c["display_name"] for c in by_type.json()["items"]] == ["web-front"]
    assert [c["display_name"] for c in by_search.json()["items"]] == ["db-ping"]


@pytest.mark.parametrize(
    "params", [{"per_page": 201}, {"page": 0}, {"check_type": "nope"}]
)
async def test_a_bad_list_query_is_refused(
    db: AsyncSession, client: AsyncClient, params: dict
) -> None:
    viewer = await _as(db, "viewer")
    assert (await client.get(CHECKS, params=params, headers=viewer)).status_code == 422


async def test_an_unknown_check_is_not_found(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    response = await client.get(f"{CHECKS}/{uuid4()}", headers=viewer)
    assert response.status_code == 404


async def test_a_viewer_never_sees_a_database_password_or_a_script(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    editor = await _as(db, "editor")
    agent = await _agent(db)
    pg = await _check(
        db, agent, check_type="postgres", target="db.internal", connection_string=DSN
    )
    script = await _check(
        db,
        agent,
        check_type="synthetic",
        target="https://a.example",
        script_code="secret_script()",
    )

    seen = (await client.get(f"{CHECKS}/{pg.id}", headers=viewer)).json()
    listed = (
        await client.get(CHECKS, params={"agent_id": str(agent.id)}, headers=viewer)
    ).text
    hidden = (await client.get(f"{CHECKS}/{script.id}", headers=viewer)).json()

    assert seen["connection_string"] == "postgresql://***@db.internal:5432/app"
    assert "s3cret-pw" not in listed and "secret_script" not in listed
    assert hidden["script_code"] is None
    full = (await client.get(f"{CHECKS}/{pg.id}", headers=editor)).json()
    assert full["connection_string"] == DSN


async def test_a_viewer_cannot_write(db: AsyncSession, client: AsyncClient) -> None:
    viewer = await _as(db, "viewer")
    agent = await _agent(db)
    check = await _check(db, agent)
    body = {
        "agent_id": str(agent.id),
        "display_name": "x",
        "check_type": "ping",
        "target": "127.0.0.1",
    }

    refused = [
        await client.post(CHECKS, json=body, headers=viewer),
        await client.patch(
            f"{CHECKS}/{check.id}", json={"display_name": "y"}, headers=viewer
        ),
        await client.delete(f"{CHECKS}/{check.id}", headers=viewer),
        await client.post(f"{CHECKS}/{check.id}/clone", headers=viewer),
        await client.post(f"{CHECKS}/{check.id}/disable", headers=viewer),
    ]

    assert [r.status_code for r in refused] == [403] * 5


async def test_an_editor_creates_changes_clones_toggles_and_deletes_a_check(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)

    created = await client.post(
        CHECKS,
        json={
            "agent_id": str(agent.id),
            "display_name": "api-made",
            "check_type": "ping",
            "target": "127.0.0.1",
        },
        headers=editor,
    )
    assert created.status_code == 201, created.text
    check_id = created.json()["id"]
    assert created.json()["agent_id"] == str(agent.id)

    changed = await client.patch(
        f"{CHECKS}/{check_id}", json={"display_name": "renamed"}, headers=editor
    )
    assert changed.status_code == 200 and changed.json()["display_name"] == "renamed"

    off = await client.post(f"{CHECKS}/{check_id}/disable", headers=editor)
    on = await client.post(f"{CHECKS}/{check_id}/enable", headers=editor)
    assert (off.json()["enabled"], on.json()["enabled"]) == (False, True)

    clone = await client.post(
        f"{CHECKS}/{check_id}/clone", json={"display_name": "copy"}, headers=editor
    )
    assert clone.status_code == 201, clone.text
    assert clone.json()["id"] != check_id
    assert (clone.json()["display_name"], clone.json()["agent_id"]) == (
        "copy",
        str(agent.id),
    )

    gone = await client.delete(f"{CHECKS}/{check_id}", headers=editor)
    assert gone.status_code == 204
    assert (await client.get(f"{CHECKS}/{check_id}", headers=editor)).status_code == 404


async def test_a_check_for_an_unknown_agent_is_not_found(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    response = await client.post(
        CHECKS,
        json={
            "agent_id": str(uuid4()),
            "display_name": "x",
            "check_type": "ping",
            "target": "127.0.0.1",
        },
        headers=editor,
    )
    assert response.status_code == 404


async def test_the_api_refuses_what_the_ssrf_policy_refuses(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    response = await client.post(
        CHECKS,
        json={
            "agent_id": str(agent.id),
            "display_name": "meta",
            "check_type": "http",
            "target": "http://169.254.169.254/latest/meta-data/",
        },
        headers=editor,
    )
    assert response.status_code in (400, 422), response.text


async def test_only_an_admin_writes_a_synthetic_check(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    agent = await _agent(db)
    response = await client.post(
        CHECKS,
        json={
            "agent_id": str(agent.id),
            "display_name": "flow",
            "check_type": "synthetic",
            "target": "https://a.example",
            "script_code": "await page.goto(target)",
        },
        headers=editor,
    )
    assert response.status_code == 403
