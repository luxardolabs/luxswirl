"""Credential routes refuse past their limit, not just declare one.

Every agent-protocol route checks a submitted key (the agent's own, or the
registration key) in a dependency. The limit sits on that dependency, so a
guess that fails still counts: past the limit the next attempt is a 429,
whatever the key. A limit on the handler would never see the refused calls,
and one address could guess keys forever.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import bcrypt
import pytest
import pytest_asyncio
from app.core import security
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import current_rate_limits, limiter, update_rate_limits
from app.main import app
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from fastapi import HTTPException
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


# Routes where a wrong key ends at the registration-key check, so the
# registration budget is the one that runs out: the heartbeat falls back to
# that key when the agent's own key is refused.
_REGISTRATION_KEYED = {"register", "recover-key", "heartbeat"}


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


@pytest.mark.parametrize("approval_status", ["active", "pending"])
async def test_wrong_registration_keys_on_the_heartbeat_spend_the_registration_budget(
    client: AsyncClient,
    db: AsyncSession,
    registration_key: str,
    approval_status: str,
) -> None:
    # An active agent reaches the registration key once its own key is
    # refused; a pending one has no key of its own.
    own_key = (
        bcrypt.hashpw(b"the-right-key", bcrypt.gensalt(rounds=4)).decode()
        if approval_status == "active"
        else None
    )
    agent = make_agent(approval_status=approval_status, api_key_hash=own_key)
    db.add(agent)
    await db.flush()
    allowed = parse(settings.security.registration_rate_limit).amount

    wrong = [
        (
            await client.post(
                "/api/v1/agent/heartbeat", json=_beat(agent.id), headers=_WRONG
            )
        ).status_code
        for _ in range(allowed + 1)
    ]
    right = await client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(agent.id),
        headers={"Authorization": f"Bearer {registration_key}"},
    )

    assert wrong == [401] * allowed + [429], wrong
    assert right.status_code == 429, right.text
    assert int(right.headers["Retry-After"]) > 0


async def test_one_agent_s_refused_keys_never_shut_out_another_at_the_same_address(
    client: AsyncClient, db: AsyncSession, registration_key: str
) -> None:
    # An agent still on a revoked key heartbeats every interval and is refused;
    # a pending agent on the same host must still reach its approval.
    stale = make_agent(approval_status="pending", api_key_hash=None)
    pending = make_agent(approval_status="pending", api_key_hash=None)
    db.add_all([stale, pending])
    await db.flush()
    allowed = parse(settings.security.registration_rate_limit).amount

    for _ in range(allowed + 1):
        await client.post(
            "/api/v1/agent/heartbeat", json=_beat(stale.id), headers=_WRONG
        )
    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(pending.id),
        headers={"Authorization": f"Bearer {registration_key}"},
    )

    assert resp.status_code == 200, resp.text


async def test_more_agent_ids_never_buy_one_address_more_than_twice_the_budget(
    client: AsyncClient, db: AsyncSession
) -> None:
    # An unnamed agent's id is a /metrics label: knowing many ids must not
    # multiply the guesses one address may make.
    agents = [
        make_agent(approval_status="pending", api_key_hash=None) for _ in range(4)
    ]
    db.add_all(agents)
    await db.flush()
    allowed = parse(settings.security.registration_rate_limit).amount

    statuses = [
        (
            await client.post(
                "/api/v1/agent/heartbeat", json=_beat(agent.id), headers=_WRONG
            )
        ).status_code
        for agent in agents
        for _ in range(allowed)
    ]

    assert statuses.count(401) == 2 * allowed, statuses
    assert statuses[2 * allowed :] == [429] * (len(statuses) - 2 * allowed)


async def test_a_concurrent_burst_of_wrong_keys_gets_no_more_checks_than_the_budget(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each check awaits the database; a burst must not all pass a count that
    # none of them has made yet.
    async def slow_refusal(authorization: str | None, db: AsyncSession) -> str:
        await asyncio.sleep(0.01)
        raise HTTPException(status_code=401, detail="Invalid registration token")

    monkeypatch.setattr(security, "verify_registration_token", slow_refusal)
    agent = make_agent(approval_status="pending", api_key_hash=None)
    allowed = parse(settings.security.registration_rate_limit).amount

    async def one() -> int:
        try:
            await security.authenticate_heartbeat(
                agent, "Bearer wrong", db, "198.51.100.99"
            )
        except HTTPException as refused:
            return refused.status_code
        return 200

    burst = await asyncio.gather(*(one() for _ in range(50)))

    assert burst.count(401) == allowed, burst
    assert burst.count(429) == 50 - allowed, burst


async def test_every_part_of_a_multi_part_registration_limit_holds_on_the_heartbeat(
    client: AsyncClient, db: AsyncSession
) -> None:
    before = current_rate_limits().registration
    update_rate_limits(registration="100/day;2/minute")
    try:
        agent = make_agent(approval_status="pending", api_key_hash=None)
        db.add(agent)
        await db.flush()

        statuses = [
            (
                await client.post(
                    "/api/v1/agent/heartbeat", json=_beat(agent.id), headers=_WRONG
                )
            ).status_code
            for _ in range(3)
        ]
    finally:
        update_rate_limits(registration=before)

    assert statuses == [401, 401, 429], statuses


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
