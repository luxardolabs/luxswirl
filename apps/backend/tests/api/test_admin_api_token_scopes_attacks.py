"""A token never reaches another family's data through its own family's routes.

Creating checks from a job needs checks:write too; a bulk create that assigns
alerts needs alerts:write; an agent key, which reads the agent's checks with
their credentials, needs agents:admin and checks:admin; deleting an agent,
which deletes its checks, needs checks:write. The gate itself refuses odd
methods and odd scope strings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.check_model import Check
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_alert, make_check, make_user
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

ADMIN = "/api/v1/admin"
SCAN_RESULT = {
    "discovered_hosts": [
        {"ip": "192.168.7.10", "hostname": None, "open_ports": [22, 80, 5432]},
        {"ip": "192.168.7.11", "hostname": "nas", "open_ports": [443]},
    ],
    "scan_params": {"timeout": 10, "ports": [22, 80, 443, 5432], "max_concurrent": 100},
}


@pytest_asyncio.fixture
async def client(monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _token(db: AsyncSession, role: str, scopes: list[str]) -> dict[str, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role, scopes=scopes)
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _agent(db: AsyncSession) -> UUID:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    return agent.id


# ── jobs:write does not create checks ───────────────────────────────────────


async def test_control_jobs_token_cannot_post_checks(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "editor", ["jobs:read", "jobs:write"])
    resp = await client.post(f"{ADMIN}/checks/{uuid4()}/enable", headers=token)
    assert resp.status_code == 403, resp.text
    assert "checks:write" in resp.text


async def test_jobs_scoped_token_creates_checks(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "editor", ["jobs:read", "jobs:write"])
    job = Job(
        job_type=JobType.NETWORK_SCAN,
        agent_id=await _agent(db),
        params={"subnet": "192.168.7.0/24"},
        status=JobStatus.COMPLETED,
        result=SCAN_RESULT,
    )
    db.add(job)
    await db.flush()
    target = await _agent(db)

    resp = await client.post(
        f"{ADMIN}/jobs/{job.id}/checks",
        json={"kind": "web", "agent_id": str(target)},
        headers=token,
    )

    db.expire_all()
    made = (await db.scalars(select(Check).where(Check.agent_id == target))).all()
    # A token without checks:write must not create checks.
    assert resp.status_code == 403 and made == [], (resp.status_code, len(made))


# ── checks:write does not assign alerts ─────────────────────────────────────


async def test_control_checks_token_cannot_set_alert_checks(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "editor", ["checks:read", "checks:write"])
    alert = make_alert()
    db.add(alert)
    await db.flush()
    resp = await client.put(
        f"{ADMIN}/alerts/{alert.id}/checks", json={"check_ids": []}, headers=token
    )
    assert resp.status_code == 403, resp.text


async def test_checks_scoped_token_attaches_checks_to_alerts(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "editor", ["checks:read", "checks:write"])
    alert = make_alert()
    db.add(alert)
    await db.flush()
    resp = await client.post(
        f"{ADMIN}/checks/bulk/create",
        json={
            "agent_id": str(await _agent(db)),
            "items": [{"url": "https://one.test"}],
            "alert_ids": [str(alert.id)],
        },
        headers=token,
    )
    queued_alerts: list[str] = []
    if resp.status_code == 202:
        job = await db.get(MaintenanceJob, UUID(resp.json()["id"]))
        assert job is not None
        queued_alerts = job.params.get("alert_ids", [])
    # A token without alerts:write must not attach checks to an alert.
    assert queued_alerts == [], (resp.status_code, queued_alerts)


# ── agents:write does not issue a key that reads checks ─────────────────────


async def test_control_agents_token_cannot_list_checks(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "editor", ["agents:read", "agents:write"])
    resp = await client.get(f"{ADMIN}/checks", headers=token)
    assert resp.status_code == 403, resp.text


async def test_agents_scoped_token_reads_check_config_via_agent_key(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "editor", ["agents:read", "agents:write"])
    agent_id = await _agent(db)
    check = make_check(
        agent_id=agent_id,
        check_type="postgres",
        target="postgresql://ro:S3cretPw@db.internal:5432/app",
    )
    db.add(check)
    await db.flush()

    key = await client.post(f"{ADMIN}/agents/{agent_id}/key", headers=token)
    leaked: list[str] = []
    listed_status = None
    if key.status_code == 201:
        api_key = key.json()["api_key"]
        listed = await client.get(
            "/api/v1/agent/checks",
            params={"agent_id": str(agent_id)},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        listed_status = listed.status_code
        if listed.status_code == 200:
            leaked = [str(c.get("target", "")) for c in listed.json()["checks"]]
    # A token without checks:read must not obtain check targets/credentials.
    assert not any("S3cretPw" in t for t in leaked), (
        key.status_code,
        listed_status,
        leaked,
    )


# ── controls on the gate itself ─────────────────────────────────────────────


async def test_control_head_and_trailing_slash_and_override(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "admin", ["*:read"])
    head = await client.request("HEAD", f"{ADMIN}/checks", headers=token)
    slash = await client.post(f"{ADMIN}/checks/{uuid4()}/enable/", headers=token)
    override = await client.post(
        f"{ADMIN}/checks/{uuid4()}/enable",
        headers={**token, "X-HTTP-Method-Override": "GET"},
    )
    assert head.status_code in (403, 405), head.status_code
    assert slash.status_code in (307, 403, 404, 405), slash.status_code
    assert override.status_code == 403, override.status_code


@pytest.mark.parametrize(
    "scope", ["checks:ＲＥＡＤ", "chеcks:read", "*:*:*", "checks:read:x", "*:"]
)
def test_control_odd_scopes_are_refused(scope: str) -> None:
    with pytest.raises(ValidationError):
        ApiTokenCreate(name="x", scopes=[scope])


@pytest.mark.parametrize(
    ("scope", "stored"),
    [("checks:read\n", "checks:read"), (" *", "*"), ("*:read ", "*:read")],
)
def test_control_whitespace_is_stripped_not_widened(scope: str, stored: str) -> None:
    assert ApiTokenCreate(name="x", scopes=[scope]).scopes == [stored]


async def test_an_agents_token_cannot_delete_an_agent_s_checks(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "editor", ["agents:read", "agents:write"])
    agent = make_agent()
    db.add(agent)
    await db.flush()

    resp = await client.delete(f"{ADMIN}/agents/{agent.id}", headers=token)

    assert resp.status_code == 403, resp.text
    assert "checks:write" in resp.text
