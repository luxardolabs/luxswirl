"""A history bucket carries what judging its errors reads, once per value.

A failing DNS check through a caching resolver stores different metrics on every
run (the TTL counts down); the bucket carries each nameserver once, not every
run's metrics. Stored metrics of any shape read as a nameserver or as none.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.crud.check_result_crud import CheckResultCRUD
from app.main import app
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

CHECKS = "/api/v1/admin/checks"
NS_PATH = "doh-path-marker-r3"
NS = f"https://doh.example.net/dns-query/{NS_PATH}"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _token(db: AsyncSession, role: str) -> dict[str, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role, scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _dns_check(db: AsyncSession):
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        check_type="dns",
        target="example.org",
        check_config={"nameserver": NS, "record_type": "A"},
    )
    db.add(check)
    await db.flush()
    return agent, check


def _expect_value_failure_metrics(ttl: int, *, cname: bool = True) -> str:
    """A DNS run that got an answer but not the expected value, through a caching
    resolver: its TTL counts down, so every run's metrics text differs. The agent
    adds `canonical_name` only when the answer came through a CNAME."""
    dns: dict[str, object] = {
        "target_domain": "example.org",
        "record_type": "A",
        "nameserver": NS,
        "port": 53,
        "record_count": 2,
        "records": ["192.0.2.10", "192.0.2.11"],
        "ttl": ttl,
        "authoritative": False,
        "truncated": False,
        "recursion_desired": True,
        "recursion_available": True,
    }
    if cname:
        dns["canonical_name"] = "edge.example.org."
    return json.dumps({"dns": dns}, separators=(",", ":"))


RUNS = 300


async def _failing_dns_runs_in_one_wide_bucket(db: AsyncSession):
    """RUNS failed runs, every 20 s, all inside one 2-hour bucket (hours=720)."""
    agent, check = await _dns_check(db)
    now = utc_now()
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=6)
    if start.hour % 2:
        start -= timedelta(hours=1)
    for i in range(RUNS):
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=start + timedelta(seconds=20 * i),
                success=False,
                error="DNS record does not contain expected value: 192.0.2.99",
                metrics=_expect_value_failure_metrics(3600 - i),
            )
        )
    await db.flush()
    return check


async def test_control_one_bucket_holds_every_run(db: AsyncSession) -> None:
    check = await _failing_dns_runs_in_one_wide_bucket(db)

    rows = await CheckResultCRUD.get_bucketed_history(db, check.id, 720)

    assert len(rows) == 1
    assert rows[0].errors == ["DNS record does not contain expected value: 192.0.2.99"]


async def test_a_bucket_carries_each_nameserver_once(
    db: AsyncSession,
) -> None:
    """One nameserver in the bucket however many failed runs name it."""
    check = await _failing_dns_runs_in_one_wide_bucket(db)

    rows = await CheckResultCRUD.get_bucketed_history(db, check.id, 720)

    assert rows[0].nameservers == [json.dumps(NS)]


ODD_METRICS = [
    "NaN",
    '["nameserver"]',
    '"nameserver"',
    '{"dns":"nameserver"}',
    '{"dns":{"nameserver":5}}',
    '{"dns":{"nameserver":""}}',
    '{"dns":{"nameserver":null}}',
    '{"dns":{"nameserver":["x"]}}',
    '{"nameserver":"https://doh.example.net/q"',
    '{"response":{"headers":{"nameserver":"ns1.example.net"}}}',
    '{"dns":{"nameserver":"https://doh.example.net/dns-query/' + NS_PATH + '"}}',
    '{"dns":{"nameserver":"\\u0000"}}',
    '{"dns":{"nameserver":"[::1"}}',
    '{"dns":{"nameserver":"https://[::1/x"}}',
    '{"dns":{"nameserver":"%"}}',
]


async def _odd_rows(db: AsyncSession):
    agent, check = await _dns_check(db)
    minute = utc_now().replace(second=0, microsecond=0) - timedelta(minutes=5)
    # The oldest bucket (the panel's history[0], which the DNS card reads) is an
    # ordinary answered run, so the card renders and only the odd rows differ.
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            timestamp=minute - timedelta(minutes=10),
            success=True,
            metrics=_expect_value_failure_metrics(60),
        )
    )
    for i, metrics in enumerate(ODD_METRICS):
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=minute + timedelta(seconds=i * 3),
                success=False,
                error=f"DNS query to {NS} failed #{i}",
                metrics=metrics,
            )
        )
    await db.flush()
    return check


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_odd_stored_metrics_do_not_500_history(
    db: AsyncSession, client: AsyncClient, role: str
) -> None:
    headers = await _token(db, role)
    check = await _odd_rows(db)

    resp = await client.get(
        f"{CHECKS}/{check.id}/history", params={"hours": 1}, headers=headers
    )

    assert resp.status_code == 200, resp.text
    if role == "viewer":
        assert NS_PATH not in resp.text, resp.text


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_odd_stored_metrics_do_not_500_the_panel(
    db: AsyncSession, client: AsyncClient, role: str
) -> None:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    check = await _odd_rows(db)
    _, session = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, session)

    resp = await client.get(f"/check/{check.id}")

    assert resp.status_code == 200, resp.text[:2000]
    if role == "viewer":
        assert NS_PATH not in resp.text


# The agent's metrics for a DNS run that got no answer (NXDOMAIN, timeout):
# apps/agent/app/checks/dns.py sets only nameserver and port in additional_data.
