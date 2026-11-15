"""A viewer reads each error in a history bucket with its own run's nameserver cut.

A DNS-over-HTTPS nameserver is a URL whose path can be its access token, and an
agent can quote it whole in an error. A bucket that spans a change of the
check's nameserver holds an older run whose error quotes the old one, which
neither the check's current nameserver nor the newest run's metrics name.
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
from app.main import app
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

CHECKS = "/api/v1/admin/checks"
OLD_PATH = "old-doh-path-marker"
NEW_PATH = "new-doh-path-marker"
OLD_NS = f"https://doh.example.net/dns-query/{OLD_PATH}"
NEW_NS = f"https://doh.example.net/dns-query/{NEW_PATH}"


def _dns_metrics(nameserver: str) -> str:
    """A DNS run's metrics as the agent reports them: the nameserver it used."""
    return json.dumps(
        {"dns": {"nameserver": nameserver, "record_type": "A", "record_count": 0}}
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _viewer(db: AsyncSession) -> dict[str, str]:
    user = make_user(role="viewer")
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="viewer", scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _dns_check_whose_nameserver_changed(db: AsyncSession):
    """A DNS check now on NEW_NS, with, in one minute, a run on OLD_NS (before the
    edit reached the agent) and a newer run on NEW_NS, both failed with errors
    quoting the nameserver whole, as the released agent writes them."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        check_type="dns",
        target="example.org",
        check_config={"nameserver": NEW_NS, "record_type": "A"},
    )
    db.add(check)
    await db.flush()
    minute = utc_now().replace(second=0, microsecond=0) - timedelta(minutes=5)
    for seconds, nameserver in ((10, OLD_NS), (40, NEW_NS)):
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=minute + timedelta(seconds=seconds),
                success=False,
                error=f"DNS query to {nameserver} failed: HTTP 502",
                metrics=_dns_metrics(nameserver),
            )
        )
    await db.flush()
    return check


async def test_control_results_list_cuts_each_runs_own_nameserver(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _viewer(db)
    check = await _dns_check_whose_nameserver_changed(db)

    resp = await client.get(f"{CHECKS}/{check.id}/results", headers=viewer)

    assert resp.status_code == 200, resp.text
    assert OLD_PATH not in resp.text, resp.text
    assert NEW_PATH not in resp.text, resp.text
    assert "https://doh.example.net" in resp.text


async def test_history_bucket_cuts_an_older_runs_own_nameserver(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _viewer(db)
    check = await _dns_check_whose_nameserver_changed(db)

    resp = await client.get(
        f"{CHECKS}/{check.id}/history", params={"hours": 1}, headers=viewer
    )

    assert resp.status_code == 200, resp.text
    assert NEW_PATH not in resp.text, resp.text
    assert OLD_PATH not in resp.text, resp.text


async def test_control_an_editor_reads_both_errors_whole_in_the_bucket(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = make_user(role="editor")
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="editor", scopes=["*"])
    )
    check = await _dns_check_whose_nameserver_changed(db)

    resp = await client.get(
        f"{CHECKS}/{check.id}/history",
        params={"hours": 1},
        headers={"Authorization": f"Bearer {plaintext}"},
    )

    assert resp.status_code == 200, resp.text
    errors = [p["error"] for p in resp.json()["points"] if p.get("error")]
    assert len(errors) == 1, errors
    assert set(errors[0].split("; ")) == {
        f"DNS query to {OLD_NS} failed: HTTP 502",
        f"DNS query to {NEW_NS} failed: HTTP 502",
    }, errors
