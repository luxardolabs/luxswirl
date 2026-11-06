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
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import bcrypt
import pytest
import pytest_asyncio
from app.core import security
from app.core.agent_key import AGENT_KEY_PREFIX
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import current_rate_limits, limiter, update_rate_limits
from app.main import app
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from app.shared.agent_protocol import AGENT_KEY_REFUSED, REFUSAL_HEADER
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


# ── A key in the agent-key format cannot be a registration key ────────────────


async def _agent_key_format_token(db: AsyncSession) -> str:
    """A key the server really issued, here to an agent no test heartbeats as."""
    issuer = make_agent(approval_status="active", api_key_hash=None)
    db.add(issuer)
    await db.flush()
    _, key = await AgentCoreService.generate_agent_key(db, issuer.id)
    return key


async def test_a_stale_key_keeps_its_marked_401_and_spends_nothing(
    client: AsyncClient, db: AsyncSession
) -> None:
    stale = await _agent_key_format_token(db)
    agent = make_agent(
        approval_status="active",
        api_key_hash=bcrypt.hashpw(b"the-right-key", bcrypt.gensalt(rounds=4)).decode(),
    )
    db.add(agent)
    await db.flush()
    allowed = parse(settings.security.registration_rate_limit).amount

    answers = [
        await client.post(
            "/api/v1/agent/heartbeat",
            json=_beat(agent.id),
            headers={"Authorization": f"Bearer {stale}"},
        )
        for _ in range(allowed + 1)
    ]
    guesses = [
        (
            await client.post(
                "/api/v1/agent/heartbeat", json=_beat(agent.id), headers=_WRONG
            )
        ).status_code
        for _ in range(allowed + 1)
    ]

    assert [a.status_code for a in answers] == [401] * (allowed + 1)
    assert {a.headers[REFUSAL_HEADER] for a in answers} == {AGENT_KEY_REFUSED}
    # The budget is whole, and still spent by what could be a registration key.
    assert guesses == [401] * allowed + [429], guesses


async def test_a_revoked_key_keeps_its_marked_401_and_spends_nothing(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(approval_status="active", api_key_hash=None)
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.generate_agent_key(db, agent.id)
    await AgentCoreService.revoke_agent_key(db, agent.id)
    allowed = parse(settings.security.registration_rate_limit).amount

    answers = [
        await client.post(
            "/api/v1/agent/heartbeat",
            json=_beat(agent.id),
            headers={"Authorization": f"Bearer {key}"},
        )
        for _ in range(allowed + 1)
    ]

    assert [a.status_code for a in answers] == [401] * (allowed + 1)
    assert {a.headers[REFUSAL_HEADER] for a in answers} == {AGENT_KEY_REFUSED}


@pytest.mark.parametrize("approval_status", ["pending", "rejected"])
async def test_an_unbudgeted_refusal_does_not_say_what_state_the_agent_is_in(
    client: AsyncClient, db: AsyncSession, approval_status: str
) -> None:
    key = await _agent_key_format_token(db)
    agent = make_agent(approval_status=approval_status, api_key_hash=None)
    db.add(agent)
    await db.flush()
    allowed = parse(settings.security.registration_rate_limit).amount

    answers = [
        await client.post(
            "/api/v1/agent/heartbeat",
            json=_beat(agent.id),
            headers={"Authorization": f"Bearer {key}"},
        )
        for _ in range(allowed + 1)
    ]

    assert [a.status_code for a in answers] == [401] * (allowed + 1)
    assert {a.headers[REFUSAL_HEADER] for a in answers} == {AGENT_KEY_REFUSED}


async def test_a_refused_agent_format_token_on_an_agent_with_no_key_is_logged(
    client: AsyncClient, db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    key = await _agent_key_format_token(db)
    agent = make_agent(approval_status="pending", api_key_hash=None)
    db.add(agent)
    await db.flush()

    with caplog.at_level(logging.WARNING, logger="luxswirl.security"):
        resp = await client.post(
            "/api/v1/agent/heartbeat",
            json=_beat(agent.id),
            headers={"Authorization": f"Bearer {key}"},
        )

    assert resp.status_code == 401, resp.text
    refused = [r for r in caplog.records if "agent-key format" in r.getMessage()]
    assert refused, [r.getMessage() for r in caplog.records]
    assert key not in caplog.text


@pytest.mark.parametrize("approval_status", ["pending", "active"])
async def test_with_auth_off_an_agent_format_token_is_not_refused(
    client: AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    approval_status: str,
) -> None:
    # `active` is a revoked agent: with auth off it learns its key is gone from this
    # 200 and registers again, as an agent holding no secret must.
    key = await _agent_key_format_token(db)
    agent = make_agent(approval_status=approval_status, api_key_hash=None)
    db.add(agent)
    await db.flush()
    monkeypatch.setattr(settings.security, "auth_enabled", False)

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(agent.id),
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_key_accepted"] is False, resp.text


def test_no_registration_key_is_in_the_agent_key_format() -> None:
    key = RegistrationKeyCoreService.generate_key()

    assert not key.startswith(AGENT_KEY_PREFIX), key


# ── A pending agent's own credential: its recovery secret ─────────────────────


async def _registered(client: AsyncClient, registration_key: str) -> tuple[UUID, str]:
    """A pending agent registered for real, and the recovery secret it was given."""
    resp = await client.post(
        "/api/v1/agent/register",
        json={"hostname": "secret-holder"},
        headers={"Authorization": f"Bearer {registration_key}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return UUID(body["agent_id"]), body["recovery_secret"]


async def test_a_pending_agent_heartbeats_with_its_recovery_secret_alone(
    client: AsyncClient, registration_key: str
) -> None:
    agent_id, secret = await _registered(client, registration_key)
    allowed = parse(settings.security.registration_rate_limit).amount

    statuses = [
        (
            await client.post(
                "/api/v1/agent/heartbeat",
                json=_beat(agent_id),
                headers={"X-Agent-Recovery-Secret": secret},
            )
        ).status_code
        for _ in range(allowed * 3)
    ]

    assert set(statuses) == {200}, statuses


async def test_wrong_recovery_secrets_are_refused_then_limited(
    client: AsyncClient, registration_key: str
) -> None:
    agent_id, secret = await _registered(client, registration_key)
    allowed = parse(settings.security.registration_rate_limit).amount

    wrong = [
        (
            await client.post(
                "/api/v1/agent/heartbeat",
                json=_beat(agent_id),
                headers={"X-Agent-Recovery-Secret": "luxswirl_rs_" + "0" * 64},
            )
        ).status_code
        for _ in range(allowed + 1)
    ]
    right = await client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(agent_id),
        headers={"X-Agent-Recovery-Secret": secret},
    )

    assert wrong == [401] * allowed + [429], wrong
    assert right.status_code == 429, right.text


async def test_a_wrong_recovery_secret_is_refused_whatever_registration_key_it_carries(
    client: AsyncClient, registration_key: str
) -> None:
    # The secret is the agent's own credential: the shared key never stands
    # in for a secret that was presented and is wrong.
    agent_id, _ = await _registered(client, registration_key)

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(agent_id),
        headers={
            "Authorization": f"Bearer {registration_key}",
            "X-Agent-Recovery-Secret": "luxswirl_rs_" + "0" * 64,
        },
    )

    assert resp.status_code == 401, resp.text


async def test_another_agent_s_recovery_secret_is_refused(
    client: AsyncClient, registration_key: str
) -> None:
    mine, _ = await _registered(client, registration_key)
    _, theirs = await _registered(client, registration_key)

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(mine),
        headers={"X-Agent-Recovery-Secret": theirs},
    )

    assert resp.status_code == 401, resp.text


async def test_a_recovery_secret_never_rewrites_a_pending_agent_that_holds_a_key(
    client: AsyncClient, db: AsyncSession, registration_key: str
) -> None:
    # An admin can issue a pending agent its key; from then on only that key
    # updates the agent.
    agent_id, secret = await _registered(client, registration_key)
    await AgentCoreService.generate_agent_key(db, agent_id)

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json={**_beat(agent_id), "hostname": "forged"},
        headers={"X-Agent-Recovery-Secret": secret},
    )

    assert resp.status_code == 200, resp.text
    agent = await AgentCoreService.get_agent_by_id(db, agent_id)
    await db.refresh(agent)
    assert agent.hostname == "secret-holder"


async def test_a_refused_shared_key_heartbeat_is_not_logged_as_authenticated(
    client: AsyncClient, db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    agent = make_agent(approval_status="pending", api_key_hash=None)
    db.add(agent)
    await db.flush()

    with caplog.at_level(logging.WARNING, logger="luxswirl.security"):
        resp = await client.post(
            "/api/v1/agent/heartbeat", json=_beat(agent.id), headers=_WRONG
        )

    assert resp.status_code == 401, resp.text
    assert not [
        r for r in caplog.records if "shared registration key" in r.getMessage()
    ]


async def test_a_heartbeat_on_the_shared_registration_key_is_logged_for_upgrade(
    client: AsyncClient,
    db: AsyncSession,
    registration_key: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = make_agent(approval_status="pending", api_key_hash=None)
    db.add(agent)
    await db.flush()

    with caplog.at_level(logging.WARNING, logger="luxswirl.security"):
        resp = await client.post(
            "/api/v1/agent/heartbeat",
            json=_beat(agent.id),
            headers={"Authorization": f"Bearer {registration_key}"},
        )

    assert resp.status_code == 200, resp.text
    assert any(
        "shared registration key" in r.getMessage()
        and getattr(r, "agent_id", None) == str(agent.id)
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


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
                agent, "Bearer wrong", None, db, "198.51.100.99"
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
