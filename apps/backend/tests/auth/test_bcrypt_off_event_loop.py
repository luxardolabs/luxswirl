"""Every bcrypt hash and check runs on a worker thread, never on the event loop.

The server is one process: a bcrypt-12 check (about 0.2 s) on the event loop
stalls every other request (agent reports, heartbeats, the UI, /health) for as
long, and an anonymous sign-in with an unknown username costs one. A ticker
sleeping 5 ms at a time measures the longest gap between its wake-ups while
each password or key path runs; a gap over 0.1 s is the loop blocked.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable

import bcrypt
import pytest
import pytest_asyncio
from app.core.agent_key import new_agent_key
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import verify_agent_token
from app.main import app
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.schemas.user_schema import UserCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.registration_key_core_service import (
    RegistrationKeyCoreService,
)
from app.services.core.user_core_service import UserCoreService
from fastapi import HTTPException
from fixtures.factories import make_agent, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

RIGHT = "TestPass123!"
STALL = 0.1


async def _longest_stall[T](work: Awaitable[T]) -> tuple[float, T]:
    """Await `work` while a ticker runs; the longest gap it saw, and the result."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    try:
        result = await work
    finally:
        stop.set()
        await task
    return max(gaps), result


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=f"https://{settings.allowed_hosts[0]}",
        ) as c:
            yield c
    finally:
        limiter.reset()


async def test_control_the_ticker_sees_no_stall_during_a_plain_await() -> None:
    gap, _ = await _longest_stall(asyncio.sleep(0.4))
    assert gap < STALL, gap


def test_control_one_bcrypt_check_is_long_enough_to_see() -> None:
    began = time.perf_counter()
    AuthCoreService.verify_password("x", AuthCoreService._FAKE_PASSWORD_HASH)
    assert time.perf_counter() - began > STALL


async def test_an_anonymous_sign_in_does_not_stall_the_event_loop(
    client: AsyncClient,
) -> None:
    # Once unmeasured: the route's first answer also compiles its template.
    await client.post("/login", data={"username": "nobody-warm", "password": "x"})
    gap, resp = await _longest_stall(
        client.post("/login", data={"username": "nobody-here", "password": "x"})
    )
    assert resp.status_code == 401
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by one anonymous sign-in"


async def test_a_sign_in_with_a_wrong_password_does_not_stall_the_event_loop(
    db: AsyncSession,
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()
    gap, signed_in = await _longest_stall(
        AuthCoreService().authenticate_user(db, user.username, "wrong")
    )
    assert signed_in is None
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by a sign-in"


async def test_a_password_change_does_not_stall_the_event_loop(
    db: AsyncSession,
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()
    gap, changed = await _longest_stall(
        AuthCoreService().change_password(db, user.id, RIGHT, "Br4nd-New!Pass-xyz")
    )
    assert changed is True
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by a password change"


async def test_a_password_reset_does_not_stall_the_event_loop(
    db: AsyncSession,
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()
    gap, _ = await _longest_stall(
        AuthCoreService().reset_password(db, user.id, "Br4nd-New!Pass-xyz")
    )
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by a password reset"


async def test_creating_a_user_does_not_stall_the_event_loop(
    db: AsyncSession,
) -> None:
    gap, created = await _longest_stall(
        UserCoreService().create_user(
            db, UserCreate(username="off-loop", password="Sup3rSecret!pw")
        )
    )
    assert created.username == "off-loop"
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by a user create"


async def test_an_agent_registration_key_check_does_not_stall_the_event_loop(
    db: AsyncSession,
) -> None:
    """An anonymous registration checks the key against every active key."""
    for n in range(2):
        await RegistrationKeyCoreService.create_key(
            db, RegistrationKeyCreate(name=f"off-loop-{n}")
        )
    gap, found = await _longest_stall(
        RegistrationKeyCoreService.verify_key_and_update_usage(
            db, RegistrationKeyCoreService.generate_key()
        )
    )
    assert found is None
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by a key check"


async def test_creating_a_registration_key_does_not_stall_the_event_loop(
    db: AsyncSession,
) -> None:
    gap, (_, plaintext) = await _longest_stall(
        RegistrationKeyCoreService.create_key(
            db, RegistrationKeyCreate(name="off-loop-create")
        )
    )
    assert plaintext
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by a key create"


async def _agent_key_check(authorization: str, key_hash: str) -> int:
    """The status an agent request with `authorization` is answered."""
    try:
        await verify_agent_token(make_agent(api_key_hash=key_hash), authorization)
    except HTTPException as exc:
        return exc.status_code
    return 200


@pytest.mark.parametrize("right", [False, True], ids=["wrong-key", "valid-key"])
async def test_an_agent_key_check_does_not_stall_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, right: bool
) -> None:
    """Every agent request checks its key; anyone who knows an agent id can
    send a wrong one."""
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    key = new_agent_key()
    key_hash = bcrypt.hashpw(key.encode(), bcrypt.gensalt()).decode()
    sent = key if right else new_agent_key()
    gap, code = await _longest_stall(_agent_key_check(f"Bearer {sent}", key_hash))
    assert code == (200 if right else 401)
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by an agent-key check"


async def test_approving_an_agent_does_not_stall_the_event_loop(
    db: AsyncSession,
) -> None:
    agent, _ = await AgentCoreService.register_agent(db, hostname="off-loop.test")
    await db.flush()
    gap, (_, key) = await _longest_stall(
        AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)
    )
    assert key
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by an approval"


async def test_issuing_an_agent_key_does_not_stall_the_event_loop(
    db: AsyncSession,
) -> None:
    agent, _ = await AgentCoreService.register_agent(db, hostname="off-loop.test")
    await db.flush()
    gap, (_, key) = await _longest_stall(
        AgentCoreService.generate_agent_key(db, agent.id)
    )
    assert key
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by a key issue"


async def test_regenerating_an_agent_key_does_not_stall_the_event_loop(
    db: AsyncSession,
) -> None:
    agent, _ = await AgentCoreService.register_agent(db, hostname="off-loop.test")
    await db.flush()
    await AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)
    gap, (_, key) = await _longest_stall(
        AgentCoreService.regenerate_agent_key(db, agent.id)
    )
    assert key
    assert gap < STALL, f"event loop blocked for {gap:.3f}s by a key regenerate"
