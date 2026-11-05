"""Refused-credential budgets: only refusals are counted in the window, and a
check in flight holds room only until it is settled.

Key recovery and a pending agent's heartbeat share this mechanism
(`app.core.rate_limit._reserve_refusal_count`).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.api import deps
from app.core import rate_limit
from app.core.config import settings
from app.core.rate_limit import (
    BUSY_RETRY_AFTER_SECONDS,
    IN_FLIGHT_SECONDS,
    buckets_in_flight,
    limiter,
    reserve_key_recovery_attempt,
    reset_in_flight,
)
from app.crud.agent_crud import AgentCRUD
from app.main import app
from app.services.core.agent_core_service import AgentCoreService
from fixtures.factories import make_agent
from httpx import ASGITransport, AsyncClient, Response
from limits import parse, parse_many

pytestmark = pytest.mark.integration

ALLOWED = parse(settings.security.registration_rate_limit).amount
ADDRESS = "203.0.113.7"


@pytest.fixture(autouse=True)
def _fresh_counts() -> Iterator[None]:
    limiter.reset()
    reset_in_flight()
    yield
    limiter.reset()
    reset_in_flight()


def test_refusals_alone_spend_the_window() -> None:
    for _ in range(ALLOWED):
        attempt = reserve_key_recovery_attempt(ADDRESS)
        assert not isinstance(attempt, int)
        attempt.settle(refused=True)

    wait = reserve_key_recovery_attempt(ADDRESS)

    assert isinstance(wait, int) and wait > BUSY_RETRY_AFTER_SECONDS, wait


def test_checks_in_flight_fill_the_room_left_only_until_settled() -> None:
    held = [reserve_key_recovery_attempt(ADDRESS) for _ in range(ALLOWED)]
    assert not any(isinstance(a, int) for a in held), held

    busy = reserve_key_recovery_attempt(ADDRESS)
    for attempt in held:
        assert not isinstance(attempt, int)
        attempt.settle(refused=False)
    after = [reserve_key_recovery_attempt(ADDRESS) for _ in range(ALLOWED)]

    assert busy == BUSY_RETRY_AFTER_SECONDS, busy
    assert not any(isinstance(a, int) for a in after), after


def test_a_check_settled_after_its_window_ended_frees_nothing_in_the_next() -> None:
    right = reserve_key_recovery_attempt(ADDRESS)
    assert not isinstance(right, int)
    # The window ends while the right check is in flight.
    for limit in parse_many(settings.security.registration_rate_limit):
        limiter.limiter.storage.clear(limit.key_for(*right.keys))
    for _ in range(ALLOWED - 1):
        guess = reserve_key_recovery_attempt(ADDRESS)
        assert not isinstance(guess, int)
        guess.settle(refused=True)
    right.settle(refused=False)
    last = reserve_key_recovery_attempt(ADDRESS)
    assert not isinstance(last, int)
    last.settle(refused=True)

    assert isinstance(reserve_key_recovery_attempt(ADDRESS), int)


def test_settling_twice_counts_once() -> None:
    attempt = reserve_key_recovery_attempt(ADDRESS)
    assert not isinstance(attempt, int)
    attempt.settle(refused=True)
    attempt.settle(refused=True)

    room = 0
    while not isinstance(reserve_key_recovery_attempt(ADDRESS), int):
        room += 1

    assert room == ALLOWED - 1, room


def test_a_check_never_settled_stops_holding_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])
    for _ in range(ALLOWED):
        assert not isinstance(reserve_key_recovery_attempt(ADDRESS), int)
    assert reserve_key_recovery_attempt(ADDRESS) == BUSY_RETRY_AFTER_SECONDS

    clock[0] += IN_FLIGHT_SECONDS

    assert not isinstance(reserve_key_recovery_attempt(ADDRESS), int)


def _room() -> int:
    """How many checks the address may start now (each settled not refused)."""
    held = []
    while not isinstance(attempt := reserve_key_recovery_attempt(ADDRESS), int):
        held.append(attempt)
    for attempt in held:
        attempt.settle(refused=False)
    return len(held)


def test_a_refusal_settled_after_its_room_expired_is_still_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])
    slow = reserve_key_recovery_attempt(ADDRESS)
    assert not isinstance(slow, int)
    clock[0] += IN_FLIGHT_SECONDS
    # Another check starts meanwhile, which drops the expired room.
    other = reserve_key_recovery_attempt(ADDRESS)
    assert not isinstance(other, int)
    other.settle(refused=False)

    slow.settle(refused=True)

    assert _room() == ALLOWED - 1


def test_buckets_with_nothing_in_flight_are_dropped() -> None:
    for n in range(50):
        address = f"2001:db8::{n:x}"
        for _ in range(ALLOWED):
            attempt = reserve_key_recovery_attempt(address)
            assert not isinstance(attempt, int)
            attempt.settle(refused=True)
        assert isinstance(reserve_key_recovery_attempt(address), int)

    assert buckets_in_flight() == 0


# ── Over HTTP: a fleet behind one address recovering at once ─────────────────


@pytest_asyncio.fixture
async def client(db, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _fleet(monkeypatch: pytest.MonkeyPatch, n: int) -> list[tuple[UUID, str]]:
    """n active agents with their right secrets. The registration key check,
    the row lock and the new key await, as the real ones do, so the
    recoveries are in flight together; the test client's one transaction
    cannot run real queries concurrently."""
    agents = {}
    fleet = []
    for _ in range(n):
        secret = uuid4().hex + uuid4().hex
        agent = make_agent(
            recovery_secret_hash=hashlib.sha256(secret.encode()).hexdigest()
        )
        agents[agent.id] = agent
        fleet.append((agent.id, secret))

    async def key_ok(agent_id, authorization, db) -> None:
        await asyncio.sleep(0.05)

    async def lock(db, agent_id) -> None:
        await asyncio.sleep(0.01)

    async def get(db, agent_id):
        return agents.get(agent_id)

    async def regen(db, agent_id):
        await asyncio.sleep(0.01)
        return None, "new-key"

    monkeypatch.setattr(deps, "verify_recovery_registration_key", key_ok)
    monkeypatch.setattr(AgentCRUD, "lock", staticmethod(lock))
    monkeypatch.setattr(AgentCRUD, "get_by_id_with_checks", staticmethod(get))
    monkeypatch.setattr(AgentCoreService, "regenerate_agent_key", staticmethod(regen))
    return fleet


async def _recover(client: AsyncClient, agent_id: UUID, secret: str) -> Response:
    return await client.post(
        f"/api/v1/agent/{agent_id}/recover-key",
        headers={
            "Authorization": "Bearer the-registration-key",
            "X-Agent-Recovery-Secret": secret,
        },
    )


async def test_a_fleet_recovering_at_once_waits_seconds_and_spends_nothing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = _fleet(monkeypatch, 2 * ALLOWED)

    first = await asyncio.gather(*(_recover(client, a, s) for a, s in fleet))
    waited = [
        (agent, resp.headers.get("Retry-After"))
        for agent, resp in zip(fleet, first, strict=True)
        if resp.status_code != 200
    ]
    retried = [(await _recover(client, a, s)).status_code for (a, s), _ in waited]
    again = [(await _recover(client, a, s)).status_code for a, s in fleet]

    assert {r.status_code for r in first} <= {200, 429}, [r.status_code for r in first]
    assert {after for _, after in waited} <= {str(BUSY_RETRY_AFTER_SECONDS)}, waited
    assert set(retried) <= {200}, retried
    assert set(again) == {200}, again
