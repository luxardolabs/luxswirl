"""Closed-set typing meets what an older version stored, and what it exported.

Before HTTP method was enum-typed the API took any method; the agent passes the
method through, so a stored OPTIONS check ran. The enum is every standard method,
so such a row still reads: the agent still fetches its checks and the web
export still works. An import is validated per check: one entry an older export could
carry (an unknown type or method) is that check's error, and the rest import.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.views.import_export_view_service import ImportExportViewService
from fixtures.factories import make_agent, make_check
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as c:
        yield c


async def _agent_with_stored_method(db: AsyncSession, method: str):
    agent = make_agent(agent_name=f"cs-{uuid4().hex[:6]}", approval_status="pending")
    db.add(agent)
    await db.flush()
    db.add_all(
        [
            make_check(agent_id=agent.id),
            make_check(
                agent_id=agent.id,
                check_type="http",
                target="https://stored.test",
                check_config={"http_method": method},
            ),
        ]
    )
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id)
    return agent, key


STORED = ["GET", "get", "OPTIONS", "patch"]


@pytest.mark.parametrize("method", STORED)
async def test_the_agent_still_fetches_its_checks(
    db: AsyncSession, client: AsyncClient, method: str
) -> None:
    agent, key = await _agent_with_stored_method(db, method)

    resp = await client.get(
        "/api/v1/agent/checks",
        params={"agent_id": str(agent.id)},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text[:300]
    methods = {c["http_method"] for c in resp.json()["checks"]}
    assert method.upper() in methods


@pytest.mark.parametrize("method", STORED)
async def test_the_export_still_works(db: AsyncSession, method: str) -> None:
    agent, _ = await _agent_with_stored_method(db, method)

    resp = await ImportExportViewService.build_export_download(db, agent.id)
    body = b"".join([chunk async for chunk in resp.body_iterator])

    exported = json.loads(body)
    assert method.upper() in {c.get("http_method") for c in exported["checks"]}


async def test_an_import_refuses_only_the_entries_that_are_not_checks(
    db: AsyncSession,
) -> None:
    agent = make_agent(agent_name=f"cs-imp-{uuid4().hex[:6]}")
    db.add(agent)
    await db.flush()

    def entry(name: str, **fields: str) -> dict[str, str]:
        return {
            "name": name,
            "check_type": "http",
            "target": f"https://{name}.test",
        } | fields

    result = await CheckCoreService.bulk_import_checks(
        db,
        agent.id,
        [
            entry("plain", http_method="GET"),
            entry("options", http_method="options"),
            entry("bad-method", http_method="FETCH"),
            entry("bad-type", check_type="keyword"),
        ],
        overwrite=False,
        actor_is_admin=True,
    )

    assert result["created"] == 2
    assert sorted(e["check"] for e in result["errors"]) == ["bad-method", "bad-type"]
