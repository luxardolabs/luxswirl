"""The anonymous public status page: what it costs per request, how often one
address may ask for it, and what it says about checks with no current result.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.status_page_model import StatusPage
from fixtures.factories import make_agent, make_check, make_check_result
from httpx import ASGITransport, AsyncClient
from limits import parse_many
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def anonymous(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as c:
            yield c
    finally:
        limiter.reset()


@pytest_asyncio.fixture
async def agent(db: AsyncSession) -> Agent:
    row = make_agent()
    db.add(row)
    await db.flush()
    return row


async def _checks(
    db: AsyncSession, agent: Agent, results: list[bool | None]
) -> list[Check]:
    """One check per entry: a fresh result of that outcome, or none (None)."""
    checks = [make_check(agent_id=agent.id) for _ in results]
    db.add_all(checks)
    await db.flush()
    for check, success in zip(checks, results, strict=True):
        if success is not None:
            db.add(
                make_check_result(check_id=check.id, agent_id=agent.id, success=success)
            )
    await db.flush()
    return checks


async def _page(db: AsyncSession, slug: str, items: list[dict[str, Any]]) -> None:
    db.add(StatusPage(name=f"Page {slug}", slug=slug, is_public=True, items=items))
    await db.flush()


def _check_items(checks: list[Check]) -> list[dict[str, Any]]:
    return [
        {"type": "check", "order": i, "check_id": str(c.id)}
        for i, c in enumerate(checks)
    ]


class _StatementCounter:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.count += 1


async def _statements(client: AsyncClient, url: str) -> int:
    counter = _StatementCounter()
    event.listen(Engine, "before_cursor_execute", counter)
    try:
        resp = await client.get(url)
    finally:
        event.remove(Engine, "before_cursor_execute", counter)
    assert resp.status_code == 200, resp.text[:300]
    return counter.count


# ---- cost per request ------------------------------------------------------


async def test_statements_do_not_grow_with_check_items(
    anonymous: AsyncClient, db: AsyncSession, agent: Agent
) -> None:
    checks = await _checks(db, agent, [True] * 25)
    await _page(db, "one-check", _check_items(checks[:1]))
    await _page(db, "many-checks", _check_items(checks))

    one = await _statements(anonymous, "/status/one-check")
    many = await _statements(anonymous, "/status/many-checks")

    assert many - one < 5, f"1 check item: {one} statements, 25: {many}"


async def test_statements_do_not_grow_with_a_groups_checks(
    anonymous: AsyncClient, db: AsyncSession, agent: Agent
) -> None:
    checks = await _checks(db, agent, [True] * 25)
    group = {"type": "group", "order": 0, "name": "Core"}
    await _page(db, "small-group", [{**group, "checks": [str(checks[0].id)]}])
    await _page(db, "big-group", [{**group, "checks": [str(c.id) for c in checks]}])

    small = await _statements(anonymous, "/status/small-group/partial")
    big = await _statements(anonymous, "/status/big-group/partial")

    assert big - small < 5, f"group of 1: {small} statements, of 25: {big}"


# ---- rate limit ------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["", "/partial"])
async def test_one_address_is_rate_limited(
    anonymous: AsyncClient, db: AsyncSession, suffix: str
) -> None:
    await _page(db, "hammered", [])
    allowed = parse_many(settings.security.status_page_rate_limit)[0].amount

    codes = [
        (await anonymous.get(f"/status/hammered{suffix}")).status_code
        for _ in range(allowed + 1)
    ]

    assert codes[:allowed] == [200] * allowed
    assert codes[allowed] == 429


def test_limit_allows_several_pollers_behind_one_address() -> None:
    """The page polls every 20 s: three requests a minute per open viewer."""
    item = parse_many(settings.security.status_page_rate_limit)[0]
    per_minute = item.amount * 60 / item.get_expiry()

    assert per_minute >= 3 * 20


# ---- checks with no current result -----------------------------------------


async def test_a_check_with_no_result_is_unknown_not_down(
    anonymous: AsyncClient, db: AsyncSession, agent: Agent
) -> None:
    checks = await _checks(db, agent, [None])
    await _page(db, "no-data", _check_items(checks))

    body = (await anonymous.get("/status/no-data")).text

    assert "Service Outage" not in body
    assert "Status Unknown" in body


async def test_unknown_checks_are_left_out_of_the_ratio(
    anonymous: AsyncClient, db: AsyncSession, agent: Agent
) -> None:
    checks = await _checks(db, agent, [True, None])
    await _page(db, "half-known", _check_items(checks))

    body = (await anonymous.get("/status/half-known")).text

    assert "All Systems Operational" in body
    assert ">100.0%<" in body


async def test_some_down_is_degraded_and_all_known_down_is_an_outage(
    anonymous: AsyncClient, db: AsyncSession, agent: Agent
) -> None:
    mostly_down = await _checks(db, agent, [True, False, False])
    all_down = await _checks(db, agent, [False, False, None])
    await _page(db, "mostly-down", _check_items(mostly_down))
    await _page(db, "all-down", _check_items(all_down))

    mostly = (await anonymous.get("/status/mostly-down")).text
    everything = (await anonymous.get("/status/all-down")).text

    assert "Partial Service Disruption" in mostly
    assert "Service Outage" in everything


async def test_group_counts_leave_unknown_checks_out(
    anonymous: AsyncClient, db: AsyncSession, agent: Agent
) -> None:
    mixed = await _checks(db, agent, [True, None])
    silent = await _checks(db, agent, [None, None])
    await _page(
        db,
        "groups",
        [
            {
                "type": "group",
                "order": 0,
                "name": "Mixed",
                "checks": [str(c.id) for c in mixed],
            },
            {
                "type": "group",
                "order": 1,
                "name": "Silent",
                "checks": [str(c.id) for c in silent],
            },
        ],
    )

    body = (await anonymous.get("/status/groups/partial")).text

    assert "All Operational" in body
    assert "All Down" not in body
    assert "Issue" not in body
    assert "No Data" in body
