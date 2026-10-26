"""Credential routes refuse past their limit, not just declare one.

Every agent-protocol route checks a submitted key (the agent's own, or the
registration key) in a dependency. The limit sits on that dependency, so a
guess that fails still counts: past the limit the next attempt is a 429,
whatever the key. A limit on the handler would never see the refused calls,
and one address could guess keys forever.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import bcrypt
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from fixtures.factories import make_agent
from httpx import ASGITransport, AsyncClient
from limits import parse
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_WRONG = {"Authorization": "Bearer not-the-right-key"}


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        # The limiter's store outlives the test: leave it empty for the next one.
        limiter.reset()


@dataclass(frozen=True)
class _World:
    agent_id: UUID
    job_id: UUID


@pytest_asyncio.fixture
async def world(db: AsyncSession) -> _World:
    """An approved agent with its own key, and a job assigned to it, so a
    wrong key is refused by the key check itself (401), not by a missing row."""
    agent = make_agent(
        api_key_hash=bcrypt.hashpw(b"the-right-key", bcrypt.gensalt(rounds=4)).decode()
    )
    db.add(agent)
    await db.flush()
    job = Job(
        job_type=JobType.NETWORK_SCAN,
        agent_id=agent.id,
        params={"subnet": "10.0.0.0/24"},
        status=JobStatus.RUNNING,
    )
    db.add(job)
    await db.flush()
    return _World(agent_id=agent.id, job_id=job.id)


def _request(route: str, w: _World) -> tuple[str, str, dict[str, object] | None]:
    """The method, url and a valid body for `route`, so only the key is wrong."""
    agent = str(w.agent_id)
    now = utc_now().isoformat()
    requests: dict[str, tuple[str, str, dict[str, object] | None]] = {
        "register": ("POST", "/api/v1/agent/register", {"hostname": "probe"}),
        "recover-key": ("POST", f"/api/v1/agent/{agent}/recover-key", None),
        "heartbeat": (
            "POST",
            "/api/v1/agent/heartbeat",
            {"agent_id": agent, "timestamp": now, "status": "online"},
        ),
        "checks": ("GET", f"/api/v1/agent/checks?agent_id={agent}", None),
        "reports": (
            "POST",
            "/api/v1/agent/reports",
            {"agent_id": agent, "timestamp": now, "checks": []},
        ),
        "job-start": ("POST", f"/api/v1/agent/jobs/{w.job_id}/start", None),
        "job-results": (
            "POST",
            f"/api/v1/agent/jobs/{w.job_id}/results",
            {"status": "completed", "result": {}},
        ),
        "artifacts": (
            "POST",
            "/api/v1/agent/artifacts",
            {
                "agent_id": agent,
                "check_id": str(uuid4()),
                "check_result_id": str(uuid4()),
                "check_result_timestamp": now,
                "artifact_type": "screenshot",
                "content_type": "image/png",
                "filename": "shot.png",
                "data_base64": base64.b64encode(b"\x89PNG-bytes").decode(),
            },
        ),
    }
    return requests[route]


_REGISTRATION_KEYED = {"register", "recover-key"}


@pytest.mark.parametrize(
    "route",
    [
        "register",
        "recover-key",
        "heartbeat",
        "checks",
        "reports",
        "job-start",
        "job-results",
        "artifacts",
    ],
)
async def test_wrong_keys_are_refused_then_limited(
    client: AsyncClient, world: _World, route: str
) -> None:
    limit = (
        settings.security.registration_rate_limit
        if route in _REGISTRATION_KEYED
        else settings.security.agent_api_rate_limit
    )
    allowed = parse(limit).amount
    method, url, body = _request(route, world)

    statuses = [
        (await client.request(method, url, json=body, headers=_WRONG)).status_code
        for _ in range(allowed + 1)
    ]

    assert set(statuses[:allowed]) == {401}, sorted(set(statuses))
    assert statuses[allowed] == 429, statuses[allowed]


# ── The registration key, wherever it is checked ─────────────────────────────


@pytest_asyncio.fixture
async def registration_key(db: AsyncSession) -> str:
    _, token = await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="rate-limit"), created_by="tests"
    )
    return token


def _beat(agent_id: UUID) -> dict[str, object]:
    return {
        "agent_id": str(agent_id),
        "timestamp": utc_now().isoformat(),
        "status": "online",
    }


async def test_a_pending_agent_heartbeating_with_the_right_key_spends_nothing(
    client: AsyncClient, db: AsyncSession, registration_key: str
) -> None:
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    allowed = parse(settings.security.registration_rate_limit).amount
    auth = {"Authorization": f"Bearer {registration_key}"}

    statuses = [
        (
            await client.post(
                "/api/v1/agent/heartbeat", json=_beat(agent.id), headers=auth
            )
        ).status_code
        for _ in range(allowed * 3)
    ]

    assert set(statuses) == {200}, statuses


async def test_malformed_registrations_do_not_spend_the_budget(
    client: AsyncClient, registration_key: str
) -> None:
    """A body that fails validation is refused before the limit counts it or
    the key is checked, so a well-formed registration still goes through."""
    allowed = parse(settings.security.registration_rate_limit).amount
    auth = {"Authorization": f"Bearer {registration_key}"}

    malformed = [
        (
            await client.post("/api/v1/agent/register", json=[1, 2], headers=auth)
        ).status_code
        for _ in range(allowed)
    ]
    good = await client.post(
        "/api/v1/agent/register", json={"hostname": "ok"}, headers=auth
    )

    assert set(malformed) == {422}, malformed
    assert good.status_code == 201, good.text
