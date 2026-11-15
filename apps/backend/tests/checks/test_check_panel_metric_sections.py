"""The check detail panel's metric cards read the newest result, of any shape.

Each card (DNS, MySQL, PostgreSQL) shows the newest bucket in the window, and a
result whose section carries only what the agent reports on that path (a
failure, an answer with no CNAME) renders, for every role, with what it did not
report shown as not reported.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

# What the agent's DNS check reports (apps/agent/app/checks/dns.py).
DNS_FAILED = {
    "target_domain": "example.org",
    "record_type": "A",
    "nameserver": "192.0.2.53",
    "port": 53,
}
DNS_ANSWERED_NO_CNAME = {
    **DNS_FAILED,
    "record_count": 1,
    "records": ["192.0.2.10"],
    "ttl": 300,
    "authoritative": False,
    "truncated": False,
    "recursion_desired": True,
    "recursion_available": True,
}
# A MySQL or PostgreSQL section from an agent that reported only where it
# connected (an older agent, or a refusal before the query ran).
DB_MINIMAL = {"host": "db.example.org", "port": 5432, "database": "app"}


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c


async def _panel(
    client: AsyncClient,
    db: AsyncSession,
    check_type: str,
    role: str,
    sections: list[dict[str, Any]],
) -> str:
    """The panel of a `check_type` check with one result per section, an hour
    apart, oldest first."""
    agent = make_agent()
    user = make_user(role=role)
    db.add_all([agent, user])
    await db.flush()
    check = make_check(agent_id=agent.id, check_type=check_type)
    db.add(check)
    await db.flush()
    now = utc_now()
    for age, section in enumerate(reversed(sections)):
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=now - timedelta(hours=age, minutes=1),
                success=False,
                error="query failed",
                metrics=json.dumps({check_type: section}),
            )
        )
    await db.flush()
    _, session = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, session)

    resp = await client.get(f"/check/{check.id}")

    assert resp.status_code == 200, resp.text[-2000:]
    return resp.text


@pytest.mark.parametrize("role", ["viewer", "editor"])
@pytest.mark.parametrize(
    "section",
    [DNS_FAILED, DNS_ANSWERED_NO_CNAME],
    ids=["failed", "answered-no-cname"],
)
@pytest.mark.parametrize("newest", [True, False], ids=["newest", "oldest"])
async def test_the_dns_card_renders_any_shape_the_agent_reports(
    client: AsyncClient,
    db: AsyncSession,
    role: str,
    section: dict[str, Any],
    newest: bool,
) -> None:
    other = {**DNS_ANSWERED_NO_CNAME, "canonical_name": "www.example.org."}
    sections = [other, section] if newest else [section, other]

    panel = await _panel(client, db, "dns", role, sections)

    assert "DNS Query Details" in panel


@pytest.mark.parametrize("check_type", ["mysql", "postgres"])
async def test_a_database_card_renders_a_minimal_section(
    client: AsyncClient, db: AsyncSession, check_type: str
) -> None:
    panel = await _panel(client, db, check_type, "editor", [DB_MINIMAL])

    assert "db.example.org" in panel


async def test_the_dns_card_shows_the_newest_result(
    client: AsyncClient, db: AsyncSession
) -> None:
    older = {**DNS_ANSWERED_NO_CNAME, "records": ["192.0.2.111"]}
    newer = {**DNS_ANSWERED_NO_CNAME, "records": ["192.0.2.222"]}

    panel = await _panel(client, db, "dns", "editor", [older, newer])

    assert "192.0.2.222" in panel
    assert "192.0.2.111" not in panel
