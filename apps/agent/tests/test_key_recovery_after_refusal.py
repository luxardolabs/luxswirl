"""An agent whose own key the server refuses recovers a new one by itself.

When an operator regenerates or revokes an agent's key, the agent's stored key
stops working: every heartbeat it sends with that key is a 401. The agent used
to keep sending it forever (and reload it on every restart), so it never asked
for the new one. Now the server's marked 401 on a heartbeat sent with its own
key makes it drop
that key from memory and prove itself with its recovery secret instead; the
server answers `approval_status: active`, and the agent recovers a new key with
the registration key and the secret, saves it, and moves every HTTP client to
it. The file keeps the old key until a new one is saved.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from app.agent import core as core_module
from app.agent import http_client
from app.agent.core import LuxSwirlAgent
from app.agent.credentials import AgentCredentials
from app.shared.agent_protocol import (
    AGENT_KEY_REFUSED,
    RECOVERY_SECRET_REFUSED,
    REFUSAL_HEADER,
    REFUSED,
    REGISTRATION_KEY_REFUSED,
)

pytestmark = pytest.mark.unit

REG_KEY = "luxswirl_rk_shared"
SECRET = "luxswirl_rs_" + "cd" * 32
STALE_KEY = "luxswirl_ak_stale"
NEW_KEY = "luxswirl_ak_new"
SERVER_URL = "https://server.example"
HEARTBEAT = "/api/v1/agent/heartbeat"


@dataclass
class Server:
    """A fake server whose idea of the agent's key the test controls."""

    agent_id: UUID
    current_key: str | None = NEW_KEY
    recover_status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def recoveries(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/recover-key")]

    def heartbeats(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == HEARTBEAT]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        auth = request.headers.get("Authorization")
        secret = request.headers.get("X-Agent-Recovery-Secret")
        if request.url.path == HEARTBEAT:
            if self.current_key and auth == f"Bearer {self.current_key}":
                return httpx.Response(
                    200, json={"approval_status": "active", "agent_key_accepted": True}
                )
            if secret == SECRET:
                return httpx.Response(
                    200,
                    json={
                        "approval_status": "active",
                        "agent_key_accepted": False,
                        "message": "Agent approved - retrieve your agent-specific key",
                    },
                )
            # The real server marks every refusal it sends an agent.
            mark = RECOVERY_SECRET_REFUSED if secret else AGENT_KEY_REFUSED
            return httpx.Response(
                401,
                json={"detail": "Invalid agent key"},
                headers={REFUSAL_HEADER: mark},
            )
        if request.url.path == f"/api/v1/agent/{self.agent_id}/recover-key":
            if self.recover_status == 401 or auth != f"Bearer {REG_KEY}":
                return httpx.Response(
                    401,
                    json={"detail": "no"},
                    headers={REFUSAL_HEADER: REGISTRATION_KEY_REFUSED},
                )
            if self.recover_status != 200:
                return httpx.Response(self.recover_status, json={"detail": "no"})
            if secret != SECRET:
                return httpx.Response(
                    401,
                    json={"detail": "no"},
                    headers={REFUSAL_HEADER: RECOVERY_SECRET_REFUSED},
                )
            self.current_key = NEW_KEY
            return httpx.Response(200, json={"api_key": NEW_KEY, "message": "ok"})
        if request.url.path == "/api/v1/agent/checks":
            # The real server refuses a key it does not take with 401, never
            # 404 (which the agent reads as "this agent is gone").
            if self.current_key and auth == f"Bearer {self.current_key}":
                return httpx.Response(200, json={"checks": []})
            return httpx.Response(
                401,
                json={"detail": "Invalid agent key"},
                headers={REFUSAL_HEADER: REFUSED},
            )
        return httpx.Response(404)


@pytest_asyncio.fixture
async def server() -> AsyncIterator[Server]:
    fake = Server(agent_id=uuid4())
    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handle)
    )
    try:
        yield fake
    finally:
        await http_client.aclose_all()


def _agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_id: UUID
) -> LuxSwirlAgent:
    """An agent whose credentials file holds `agent_id` and a key the server
    may no longer accept, built the way the process builds it."""
    creds_path = tmp_path / "agent_credentials.json"
    if not creds_path.exists():
        AgentCredentials(str(creds_path)).save(agent_id, STALE_KEY, SECRET)
    monkeypatch.setattr(
        core_module, "AgentCredentials", lambda: AgentCredentials(str(creds_path))
    )
    config: dict[str, Any] = {
        "server_url": SERVER_URL,
        "auth_key": REG_KEY,
        "report_storage_path": str(tmp_path / "reports"),
        "heartbeat_interval": 0.01,
        "resource_monitoring_enabled": False,
    }
    agent = LuxSwirlAgent(config)
    # No 5-second CPU sample per beat.
    monkeypatch.setattr(
        agent.heartbeat_sender.process, "cpu_percent", lambda interval=None: 0.0
    )
    return agent


async def _beat_until(
    agent: LuxSwirlAgent, done: Callable[[], bool], timeout: float = 5.0
) -> None:
    """Run the heartbeat loop until `done()` holds (or `timeout` passes)."""
    sender = agent.heartbeat_sender
    sender.start()
    task = asyncio.create_task(sender.send_heartbeat())
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while not done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
    finally:
        sender.stop()
        await asyncio.wait_for(task, timeout=5.0)


async def _open_clients(agent: LuxSwirlAgent) -> None:
    await agent.reporter.connect()
    agent.job_processor.client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {agent.config['auth_key']}"}
    )


async def _close_clients(agent: LuxSwirlAgent) -> None:
    await agent.reporter.close()
    if agent.job_processor.client is not None:
        await agent.job_processor.client.aclose()


async def test_a_refused_key_is_replaced_by_a_recovered_one(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _open_clients(agent)
    try:
        assert agent.config["auth_key"] == STALE_KEY

        await _beat_until(agent, lambda: agent.credentials.api_key == NEW_KEY)

        # It asked once, with the registration key and its own secret.
        (recovery,) = server.recoveries()
        assert recovery.headers["Authorization"] == f"Bearer {REG_KEY}"
        assert recovery.headers["X-Agent-Recovery-Secret"] == SECRET
        # Every client moved to the new key.
        assert agent.config["auth_key"] == NEW_KEY
        assert agent.reporter.auth_key == NEW_KEY
        assert agent.reporter.client is not None
        assert agent.reporter.client.headers["Authorization"] == f"Bearer {NEW_KEY}"
        assert agent.job_processor.client is not None
        assert (
            agent.job_processor.client.headers["Authorization"] == f"Bearer {NEW_KEY}"
        )
        # Saved, with the secret kept for the next time.
        reloaded = AgentCredentials(str(tmp_path / "agent_credentials.json"))
        assert reloaded.load()
        assert reloaded.api_key == NEW_KEY
        assert reloaded.recovery_secret == SECRET
    finally:
        await _close_clients(agent)


async def test_heartbeats_after_recovery_carry_the_new_key(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _beat_until(agent, lambda: agent.credentials.api_key == NEW_KEY)
    server.requests.clear()

    await _beat_until(agent, lambda: len(server.heartbeats()) >= 2)

    beats = server.heartbeats()
    assert beats
    for beat in beats:
        assert beat.headers["Authorization"] == f"Bearer {NEW_KEY}"
    assert server.recoveries() == []


async def test_a_restart_after_recovery_loads_the_new_key(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _beat_until(agent, lambda: agent.credentials.api_key == NEW_KEY)

    restarted = _agent(monkeypatch, tmp_path, server.agent_id)

    assert restarted.config["auth_key"] == NEW_KEY
    assert restarted.credentials.api_key == NEW_KEY


async def test_a_refused_recovery_keeps_the_stored_key_and_asks_once(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The key on file is not dropped until a new one is saved, and a refused
    recovery is not asked again every heartbeat."""
    server.recover_status = 401
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until(agent, lambda: len(server.heartbeats()) >= 10)

    assert len(server.heartbeats()) >= 10
    assert len(server.recoveries()) == 1
    reloaded = AgentCredentials(str(tmp_path / "agent_credentials.json"))
    assert reloaded.load()
    assert reloaded.api_key == STALE_KEY
    assert reloaded.recovery_secret == SECRET


async def test_a_rate_limited_recovery_waits_out_its_retry_after(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.recover_status = 429
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until(agent, lambda: len(server.heartbeats()) >= 10)

    assert len(server.heartbeats()) >= 10
    assert len(server.recoveries()) == 1


async def test_an_agent_without_a_recovery_secret_keeps_its_key(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no secret there is nothing to prove itself with: the agent keeps its
    key and never asks for a new one (it says so in its log)."""
    creds_path = tmp_path / "agent_credentials.json"
    AgentCredentials(str(creds_path)).save(server.agent_id, STALE_KEY)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until(agent, lambda: len(server.heartbeats()) >= 3)

    assert len(server.heartbeats()) >= 3
    assert agent.credentials.api_key == STALE_KEY
    assert server.recoveries() == []
    for beat in server.heartbeats():
        assert beat.headers["Authorization"] == f"Bearer {STALE_KEY}"


async def test_an_orphaned_agent_re_registers_with_the_registration_key(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Re-registering (an orphaned agent) uses the registration key, never the
    agent's own key, which config holds once the agent has one."""
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    assert agent.config["auth_key"] == STALE_KEY

    await agent.registration.register_with_server()

    (register,) = [r for r in server.requests if r.url.path == "/api/v1/agent/register"]
    assert register.headers["Authorization"] == f"Bearer {REG_KEY}"
