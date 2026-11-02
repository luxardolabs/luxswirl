"""The agent keeps its recovery secret and proves it on recover-key.

Registration returns a per-agent recovery secret once. The agent stores it with
its agent id in the encrypted credentials file, keeps it when the agent key is
later saved, and sends it (header X-Agent-Recovery-Secret) with the
registration key when it recovers. An agent whose stored credentials predate the
secret cannot recover, so it registers afresh instead.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from app.agent import http_client
from app.agent import registration as registration_module
from app.agent.credentials import AgentCredentials
from app.agent.registration import AgentRegistration

pytestmark = pytest.mark.unit

REG_KEY = "luxswirl_rk_shared"
SECRET = "luxswirl_rs_" + "ab" * 32
SERVER_URL = "https://server.example"


class Server:
    """A fake server: registers agents and recovers keys, recording requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.registered: list[UUID] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/v1/agent/register":
            agent_id = uuid4()
            self.registered.append(agent_id)
            return httpx.Response(
                201,
                json={
                    "agent_id": str(agent_id),
                    "status": "pending",
                    "message": "registered",
                    "recovery_secret": SECRET,
                },
            )
        if request.url.path.endswith("/recover-key"):
            if request.headers.get("X-Agent-Recovery-Secret") != SECRET:
                return httpx.Response(401, json={"detail": "no"})
            return httpx.Response(
                200, json={"api_key": "luxswirl_ak_new", "message": "ok"}
            )
        return httpx.Response(404)


@pytest_asyncio.fixture
async def server() -> AsyncIterator[Server]:
    fake = Server()
    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handle)
    )
    try:
        yield fake
    finally:
        await http_client.aclose_all()


def _registration(creds: AgentCredentials) -> tuple[AgentRegistration, dict[str, Any]]:
    config: dict[str, Any] = {"server_url": SERVER_URL, "auth_key": REG_KEY}
    if creds.agent_id is not None:
        config["agent_id"] = creds.agent_id
    registration = AgentRegistration(config, creds)
    registration.hostname = "agent-host"
    return registration, config


def test_the_secret_is_stored_encrypted_and_survives_saving_the_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent_credentials.json"
    agent_id = uuid4()
    creds = AgentCredentials(str(path))

    assert creds.save(agent_id, recovery_secret=SECRET)
    assert SECRET.encode() not in path.read_bytes()
    assert creds.save(agent_id, "luxswirl_ak_key")

    reloaded = AgentCredentials(str(path))
    assert reloaded.load()
    assert reloaded.agent_id == agent_id
    assert reloaded.api_key == "luxswirl_ak_key"
    assert reloaded.recovery_secret == SECRET


def test_a_legacy_plaintext_file_keeps_its_secret_through_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent_credentials.json"
    agent_id = uuid4()
    path.write_text(json.dumps({"agent_id": str(agent_id), "recovery_secret": SECRET}))

    creds = AgentCredentials(str(path))
    assert creds.load()

    assert creds.recovery_secret == SECRET
    assert SECRET.encode() not in path.read_bytes()


async def test_registration_persists_the_secret(server: Server, tmp_path: Path) -> None:
    creds = AgentCredentials(str(tmp_path / "agent_credentials.json"))
    registration, config = _registration(creds)

    assert await registration.register_with_server()

    reloaded = AgentCredentials(str(tmp_path / "agent_credentials.json"))
    assert reloaded.load()
    assert reloaded.agent_id == server.registered[0]
    assert reloaded.recovery_secret == SECRET
    assert config["agent_id"] == server.registered[0]


async def test_recovery_sends_the_secret_and_keeps_it(
    server: Server, tmp_path: Path
) -> None:
    path = tmp_path / "agent_credentials.json"
    creds = AgentCredentials(str(path))
    agent_id = uuid4()
    creds.save(agent_id, recovery_secret=SECRET)
    registration, config = _registration(creds)

    assert await registration.recover_agent_key()

    sent = server.requests[-1]
    assert sent.url.path == f"/api/v1/agent/{agent_id}/recover-key"
    assert sent.headers["Authorization"] == f"Bearer {REG_KEY}"
    assert sent.headers["X-Agent-Recovery-Secret"] == SECRET
    assert SECRET not in str(sent.url)
    assert config["auth_key"] == "luxswirl_ak_new"
    reloaded = AgentCredentials(str(path))
    assert reloaded.load()
    assert reloaded.api_key == "luxswirl_ak_new"
    assert reloaded.recovery_secret == SECRET


async def test_credentials_without_a_secret_register_afresh_instead_of_recovering(
    server: Server, tmp_path: Path
) -> None:
    """An agent registered before secrets existed cannot recover: it never
    sends a bare registration-key recovery, it registers again."""
    path = tmp_path / "agent_credentials.json"
    creds = AgentCredentials(str(path))
    old_id = uuid4()
    creds.save(old_id)
    registration, config = _registration(creds)

    assert not await registration.recover_agent_key()

    assert not any(r.url.path.endswith("/recover-key") for r in server.requests)
    assert [r.url.path for r in server.requests] == ["/api/v1/agent/register"]
    new_id = server.registered[0]
    assert config["agent_id"] == new_id != old_id
    reloaded = AgentCredentials(str(path))
    assert reloaded.load()
    assert reloaded.agent_id == new_id
    assert reloaded.recovery_secret == SECRET


async def test_startup_re_registers_credentials_that_can_never_get_a_key(
    server: Server, tmp_path: Path
) -> None:
    """No key and no secret: approval could never be completed, so register now
    and let the operator approve the agent that can."""
    creds = AgentCredentials(str(tmp_path / "agent_credentials.json"))
    creds.save(uuid4())
    registration, _ = _registration(creds)

    assert await registration.ensure_recoverable()

    assert [r.url.path for r in server.requests] == ["/api/v1/agent/register"]
    assert creds.recovery_secret == SECRET


async def test_startup_leaves_working_credentials_alone(
    server: Server, tmp_path: Path
) -> None:
    """An agent that already holds its key keeps running on it, secret or not."""
    creds = AgentCredentials(str(tmp_path / "agent_credentials.json"))
    creds.save(uuid4(), "luxswirl_ak_working")
    registration, _ = _registration(creds)

    assert await registration.ensure_recoverable()

    assert server.requests == []
    assert creds.api_key == "luxswirl_ak_working"


async def _recoveries(status: int, headers: dict[str, str], tmp_path: Path) -> int:
    """How many recover-key requests three heartbeats make when the server
    answers every one with `status`."""
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, headers=headers, json={"detail": "no"})

    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle)
    )
    try:
        creds = AgentCredentials(str(tmp_path / "agent_credentials.json"))
        creds.save(uuid4(), recovery_secret=SECRET)
        registration, _ = _registration(creds)
        for _ in range(3):
            assert not await registration.recover_agent_key()
    finally:
        await http_client.aclose_all()
    return len(sent)


async def test_a_refused_recovery_is_not_asked_again(tmp_path: Path) -> None:
    # A 401 needs a new registration key and a restart: asking every heartbeat
    # would only spend the recover-key budget the address shares.
    assert await _recoveries(401, {}, tmp_path) == 1


async def test_a_rate_limited_recovery_waits_out_its_retry_after(
    tmp_path: Path,
) -> None:
    assert await _recoveries(429, {"Retry-After": "3600"}, tmp_path) == 1


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("5", 5.0),
        ("0", 1.0),
        ("-30", 1.0),
        ("99999999999", 3600.0),
        ("inf", 60.0),
        ("nan", 60.0),
        ("1e400", 60.0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", 60.0),
        ("", 60.0),
    ],
)
def test_a_retry_after_waits_between_a_second_and_an_hour(
    header: str, seconds: float
) -> None:
    # A value this server never sends (from a proxy, say) must not strand
    # recovery until the agent restarts.
    response = httpx.Response(429, headers={"Retry-After": header})
    assert registration_module._retry_after(response) == seconds


async def test_a_new_registration_is_not_held_back_by_an_old_refusal(
    server: Server, tmp_path: Path
) -> None:
    creds = AgentCredentials(str(tmp_path / "agent_credentials.json"))
    registration, _ = _registration(creds)
    registration._recovery_refused = True
    registration._recovery_not_before = float("inf")

    assert await registration.register_with_server()

    assert await registration.recover_agent_key()
