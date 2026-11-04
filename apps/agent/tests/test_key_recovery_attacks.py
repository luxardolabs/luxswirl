"""Attacks on recovering an agent key after a 401, each fixed.

A1: one 401 the server does not stand by (a proxy, a blip) must not make the
agent give up a key the server still accepts. A2: an agent whose key is
refused, and that cannot recover one, must not stamp its Docker liveness file.
A3: two copies of one agent must not trade the key at every heartbeat. A4: a
recovered key that cannot be saved is used, not thrown away and recovered
again at every heartbeat. A5: checks changed while the key was refused are
fetched with the recovered key.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from app.agent import core as core_module
from app.agent import heartbeat as heartbeat_module
from app.agent import http_client
from app.agent.core import LuxSwirlAgent
from app.agent.credentials import AgentCredentials

pytestmark = pytest.mark.unit

REG_KEY = "luxswirl_rk_shared"
SECRET = "luxswirl_rs_" + "cd" * 32
STALE_KEY = "luxswirl_ak_stale"
SERVER_URL = "https://server.example"
HEARTBEAT = "/api/v1/agent/heartbeat"
CHECKS = "/api/v1/agent/checks"


@dataclass
class Server:
    """Fake server. `current_key` is the agent key it accepts; a recovery
    issues a fresh key each time (as AgentCoreService.recover_agent_key does,
    via regenerate_agent_key) unless `recover_status` says otherwise."""

    agent_id: UUID
    current_key: str | None
    recover_status: int = 200
    forged_401s: int = 0  # the next N heartbeats get a 401 whatever they carry
    config_version: str = "v1"
    requests: list[httpx.Request] = field(default_factory=list)
    _counter: itertools.count = field(default_factory=lambda: itertools.count(1))

    def recoveries(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/recover-key")]

    def heartbeats(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == HEARTBEAT]

    def check_fetches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == CHECKS]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        auth = request.headers.get("Authorization")
        secret = request.headers.get("X-Agent-Recovery-Secret")
        own = self.current_key is not None and auth == f"Bearer {self.current_key}"
        if request.url.path == HEARTBEAT:
            if self.forged_401s:
                self.forged_401s -= 1
                return httpx.Response(401, text="proxy says no")
            body: dict[str, Any] = {
                "approval_status": "active",
                "config_version": self.config_version,
            }
            if own:
                return httpx.Response(200, json=body | {"agent_key_accepted": True})
            if secret == SECRET:
                return httpx.Response(
                    200,
                    json=body | {"message": "recover", "agent_key_accepted": False},
                )
            return httpx.Response(401, json={"detail": "Invalid agent key"})
        if request.url.path == CHECKS:
            if own:
                return httpx.Response(200, json={"checks": []})
            return httpx.Response(401, json={"detail": "no"})
        if request.url.path == f"/api/v1/agent/{self.agent_id}/recover-key":
            if self.recover_status != 200:
                return httpx.Response(self.recover_status, json={"detail": "no"})
            if auth != f"Bearer {REG_KEY}" or secret != SECRET:
                return httpx.Response(401, json={"detail": "no"})
            self.current_key = f"luxswirl_ak_{next(self._counter)}"
            return httpx.Response(200, json={"api_key": self.current_key})
        return httpx.Response(404)


@pytest_asyncio.fixture
async def server() -> AsyncIterator[Server]:
    fake = Server(agent_id=uuid4(), current_key=STALE_KEY)
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
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    monkeypatch.setattr(
        agent.heartbeat_sender.process, "cpu_percent", lambda interval=None: 0.0
    )
    return agent


async def _beat_until(
    agents: list[LuxSwirlAgent], done: Callable[[], bool], timeout: float = 5.0
) -> None:
    tasks = []
    for agent in agents:
        agent.heartbeat_sender.start()
        tasks.append(asyncio.create_task(agent.heartbeat_sender.send_heartbeat()))
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while not done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
    finally:
        for agent in agents:
            agent.heartbeat_sender.stop()
        for task in tasks:
            await asyncio.wait_for(task, timeout=5.0)


# --------------------------------------------------------------------------
# A1. One forged/proxied 401 makes an approved agent whose registration key
# was revoked (docs: it "keeps working ... since it uses its own key") abandon
# its STILL-VALID key until restart: every later heartbeat is a pending-
# credential one (server applies nothing -> agent shows offline, no jobs,
# check fetches 401).
# --------------------------------------------------------------------------
async def test_a1_single_forged_401_does_not_abandon_a_valid_key(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.forged_401s = 1  # one 401 from a proxy / blip; the key stays valid
    server.recover_status = 401  # registration key revoked (documented as fine)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 12)

    tail = server.heartbeats()[-5:]
    assert len(tail) == 5
    for beat in tail:
        # The key was never refused by the server: it must still be used.
        assert beat.headers.get("Authorization") == f"Bearer {STALE_KEY}"
        assert "X-Agent-Recovery-Secret" not in beat.headers


# --------------------------------------------------------------------------
# A2. Docker liveness: an agent whose own key is refused and cannot recover
# one (registration key revoked) keeps stamping its liveness file, because the
# secret-authenticated heartbeat is a 200. app/health/check.py promises
# "credentials rejected -> stops stamping -> UNHEALTHY".
# --------------------------------------------------------------------------
async def test_a2_agent_that_cannot_authenticate_is_not_stamped_live(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    health = tmp_path / "agent_health"
    monkeypatch.setattr(heartbeat_module, "HEALTH_FILE", health)
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    server.recover_status = 401
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 6)

    assert len(server.heartbeats()) >= 6
    assert agent.credentials.api_key != server.current_key
    assert not health.exists(), (
        "liveness stamped although the agent holds no key the server accepts"
    )


# --------------------------------------------------------------------------
# A3. Two processes holding the same credentials (a cloned VM / restored
# backup: same hostname + machine-id so the file decrypts). After one
# Regenerate they rotate the server's key against each other forever: each
# recovery invalidates the other's key, which then 401s and recovers again.
# No back-off applies because every recovery succeeds.
# --------------------------------------------------------------------------
async def test_a3_cloned_agents_do_not_rotate_the_key_forever(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = _agent(monkeypatch, tmp_path / "a", server.agent_id)
    b = _agent(monkeypatch, tmp_path / "b", server.agent_id)
    server.current_key = "luxswirl_ak_regenerated_by_operator"

    await _beat_until([a, b], lambda: len(server.recoveries()) >= 20, timeout=3.0)

    assert len(server.recoveries()) <= 4, (
        f"{len(server.recoveries())} key recoveries (each one a server-side "
        f"regenerate) in {len(server.heartbeats())} heartbeats"
    )


# --------------------------------------------------------------------------
# A4. The new key cannot be saved (full disk, read-only volume): the recovered
# key is thrown away and recover-key is called on EVERY heartbeat, each call
# regenerating the server-side key, with no back-off.
# --------------------------------------------------------------------------
async def test_a4_unsaveable_key_does_not_hammer_recover_key(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    creds_path = tmp_path / "agent_credentials.json"
    creds_path.unlink()
    creds_path.mkdir()  # every later save fails (write_bytes on a directory)
    server.current_key = "luxswirl_ak_regenerated_by_operator"

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 15)

    assert len(server.heartbeats()) >= 15
    assert len(server.recoveries()) <= 2, (
        f"{len(server.recoveries())} recover-key calls in "
        f"{len(server.heartbeats())} heartbeats"
    )


# --------------------------------------------------------------------------
# A5. A check change that lands with the key refusal is lost until the next
# periodic sync: the heartbeat that carries the new config_version is the
# secret-authenticated one, _sync_checks runs BEFORE _handle_agent_approval,
# so the reload goes out with the registration key (401, cache used), and
# the version is recorded as seen. After recovery nothing re-fetches.
# --------------------------------------------------------------------------
async def test_a5_config_change_during_recovery_is_fetched_with_the_new_key(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    agent.heartbeat_sender.on_config_reload = agent._reload_checks
    # Phase 1: own key valid, baseline version v1.
    await _beat_until([agent], lambda: len(server.heartbeats()) >= 2)
    assert agent.heartbeat_sender.config_version == "v1"
    # Operator regenerates the key and edits a check.
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    server.config_version = "v2"
    server.requests.clear()

    await _beat_until(
        [agent],
        lambda: (
            agent.credentials.api_key == server.current_key
            and len(server.heartbeats()) >= 10
        ),
    )

    assert agent.credentials.api_key == server.current_key
    good = [
        f
        for f in server.check_fetches()
        if f.headers.get("Authorization") == f"Bearer {server.current_key}"
    ]
    assert good, (
        "v2 checks never fetched with a working key; fetches sent: "
        f"{[f.headers.get('Authorization') for f in server.check_fetches()]}"
    )


# Control for A5's harness: a version change on an own-key heartbeat reloads
# with that key.
async def test_a5_control_version_change_reloads_with_own_key(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    agent.heartbeat_sender.on_config_reload = agent._reload_checks
    await _beat_until([agent], lambda: len(server.heartbeats()) >= 2)
    server.config_version = "v2"
    server.requests.clear()
    await _beat_until([agent], lambda: len(server.heartbeats()) >= 4)
    assert any(
        f.headers.get("Authorization") == f"Bearer {STALE_KEY}"
        for f in server.check_fetches()
    )


# Control for A1/A2/A4 harness: with no 401 at all the agent keeps its key,
# never recovers, and stamps liveness.
async def test_control_no_refusal(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    health = tmp_path / "agent_health"
    monkeypatch.setattr(heartbeat_module, "HEALTH_FILE", health)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _beat_until([agent], lambda: len(server.heartbeats()) >= 5)
    assert server.recoveries() == []
    assert all(
        b.headers["Authorization"] == f"Bearer {STALE_KEY}" for b in server.heartbeats()
    )
    assert health.exists()
