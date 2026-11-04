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
import errno
import itertools
import os
import stat
import tempfile
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
from app.agent import registration as registration_module
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
SERVER_URL = "https://server.example"
HEARTBEAT = "/api/v1/agent/heartbeat"
CHECKS = "/api/v1/agent/checks"
REGISTER = "/api/v1/agent/register"
# The header the real server marks its refusal of a recovery secret with.
MARK = {REFUSAL_HEADER: RECOVERY_SECRET_REFUSED}
# ...and its refusal of a heartbeat's key (sent without the secret).
MARK_KEY = {REFUSAL_HEADER: AGENT_KEY_REFUSED}
# ...of the registration key, and any other refusal.
MARK_REG = {REFUSAL_HEADER: REGISTRATION_KEY_REFUSED}
MARK_OTHER = {REFUSAL_HEADER: REFUSED}


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
    # False: a server from before agent_key_accepted (only its message says
    # the key did not authenticate).
    sends_key_accepted: bool = True
    # True: the key was revoked, so no key hash is left, and the server sends
    # no "retrieve your key" message (process_heartbeat).
    revoked: bool = False
    # Heartbeats the real server would answer 401 and count against the
    # agent's pending budget: a refused key with no secret beside it.
    counted_refusals: int = 0
    # True: the key was revoked, which voids the recovery secret too: a
    # heartbeat and recover-key refuse it, and only registering again helps.
    secret_voided: bool = False
    register_status: int = 201
    new_agent_id: UUID = field(default_factory=uuid4)
    new_secret: str = "luxswirl_rs_" + "ef" * 32
    # Refused heartbeat credentials the real server takes before it answers
    # 429 instead (its pending budget, 5 an hour); None: no budget.
    pending_budget: int | None = None
    _refused_heartbeats: int = 0
    requests: list[httpx.Request] = field(default_factory=list)
    _counter: itertools.count = field(default_factory=lambda: itertools.count(1))

    def recoveries(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/recover-key")]

    def heartbeats(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == HEARTBEAT]

    def check_fetches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == CHECKS]

    def registrations(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == REGISTER]

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = self._answer(request)
        if self.sends_key_accepted or REFUSAL_HEADER not in response.headers:
            return response
        # A server from before agent_key_accepted marks no refusal either.
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in (REFUSAL_HEADER.lower(), "content-length")
        }
        return httpx.Response(
            response.status_code, headers=headers, content=response.content
        )

    def _answer(self, request: httpx.Request) -> httpx.Response:
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
                if self.sends_key_accepted:
                    body["agent_key_accepted"] = True
                return httpx.Response(200, json=body)
            if secret == self.new_secret and auth == f"Bearer {REG_KEY}":
                return httpx.Response(200, json={"approval_status": "pending"})
            if secret == SECRET and not self.secret_voided:
                if not self.revoked:
                    body["message"] = "recover"
                if self.sends_key_accepted:
                    body["agent_key_accepted"] = False
                return httpx.Response(200, json=body)
            if secret is None:
                self.counted_refusals += 1
            if self.pending_budget is not None:
                self._refused_heartbeats += 1
                if self._refused_heartbeats > self.pending_budget:
                    return httpx.Response(
                        429,
                        json={"detail": "Too many refused agent credentials"},
                        headers={"Retry-After": "3600"},
                    )
            if secret is not None:
                # The real server marks its refusal of the secret.
                return httpx.Response(
                    401, json={"detail": "Invalid agent recovery secret"}, headers=MARK
                )
            return httpx.Response(
                401, json={"detail": "Invalid agent key"}, headers=MARK_KEY
            )
        if request.url.path == CHECKS:
            if own:
                return httpx.Response(200, json={"checks": []})
            return httpx.Response(401, json={"detail": "no"}, headers=MARK_OTHER)
        if request.url.path == f"/api/v1/agent/{self.agent_id}/recover-key":
            if self.recover_status == 401 or auth != f"Bearer {REG_KEY}":
                return httpx.Response(401, json={"detail": "no"}, headers=MARK_REG)
            if self.recover_status != 200:
                return httpx.Response(self.recover_status, json={"detail": "no"})
            if secret != SECRET or self.secret_voided:
                return httpx.Response(
                    401,
                    json={"error": "AUTHENTICATION_FAILED", "message": "no"},
                    headers=MARK,
                )
            self.current_key = f"luxswirl_ak_{next(self._counter)}"
            return httpx.Response(200, json={"api_key": self.current_key})
        if request.url.path == REGISTER:
            if self.register_status != 201 or auth != f"Bearer {REG_KEY}":
                status = self.register_status if self.register_status != 201 else 401
                headers = MARK_REG if status in (401, 403) else {}
                return httpx.Response(status, json={"detail": "no"}, headers=headers)
            return httpx.Response(
                201,
                json={
                    "agent_id": str(self.new_agent_id),
                    "recovery_secret": self.new_secret,
                    "status": "pending",
                },
            )
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
    assert server.recoveries() == []
    assert agent.credentials.api_key == STALE_KEY


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


# Round 2.
#
# D1. The first heartbeat after a key change carries only the old key, and
# the real server counts that 401 against the agent's pending budget (5 an
# hour). The secret is sent only after the server refused the key (the owner
# ruled it must not ride beside an accepted key, G5), so a key change costs
# the budget exactly one refusal, never one per heartbeat.
async def test_d1_a_refused_key_costs_one_counted_refusal(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: agent.credentials.api_key == server.current_key)

    assert agent.credentials.api_key == server.current_key
    assert server.counted_refusals == 1
    first, second = server.heartbeats()[:2]
    assert "X-Agent-Recovery-Secret" not in first.headers
    assert second.headers.get("X-Agent-Recovery-Secret") == SECRET


# D2 / C2. A server from before agent_key_accepted: a stray 401 must not make
# the agent replace (or stop vouching for) a key the server still accepts...
async def test_d2_an_older_server_that_accepts_the_key_keeps_it(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    health = tmp_path / "agent_health"
    monkeypatch.setattr(heartbeat_module, "HEALTH_FILE", health)
    server.sends_key_accepted = False
    server.forged_401s = 1
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 8)

    assert server.recoveries() == []
    assert agent.credentials.api_key == STALE_KEY
    assert not agent.heartbeat_sender.key_refused
    assert health.exists()


# ...and its refusal of a regenerated key, unmarked like every refusal of a
# server from before the mark, is temporary to this agent (which needs a
# server that marks): it keeps its key, never sends its secret, recovers and
# registers nothing, and reads unhealthy while the heartbeat fails.
async def test_d2_an_older_servers_unmarked_refusal_changes_nothing(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    health = tmp_path / "agent_health"
    monkeypatch.setattr(heartbeat_module, "HEALTH_FILE", health)
    server.sends_key_accepted = False
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 8)

    assert agent.credentials.api_key == STALE_KEY
    assert all("X-Agent-Recovery-Secret" not in b.headers for b in server.heartbeats())
    assert server.recoveries() == []
    assert server.registrations() == []
    assert not health.exists()


# C1. A recovered key the disk refused lived in memory only, never saved: a
# restart then had to recover again (and could not, with the registration key
# revoked). It is saved once the disk takes it.
async def test_c1_an_unsaved_key_is_saved_once_the_disk_takes_it(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    creds_path = tmp_path / "agent_credentials.json"
    creds_path.unlink()
    creds_path.mkdir()  # the save fails while this is a directory
    server.current_key = "luxswirl_ak_regenerated_by_operator"

    await _beat_until([agent], lambda: agent.credentials.api_key == server.current_key)
    assert agent.credentials.unsaved
    creds_path.rmdir()  # the disk takes writes again
    await _beat_until([agent], lambda: not agent.credentials.unsaved)

    reloaded = AgentCredentials(str(creds_path))
    assert reloaded.load()
    assert reloaded.api_key == server.current_key
    assert reloaded.recovery_secret == SECRET
    assert len(server.recoveries()) == 1


# E1. A server from before the mark, with the key REVOKED: its 401s are not
# marked, so they are temporary to this agent. It keeps its credentials,
# registers nothing over them, and never reads live while refused.
async def test_e1_an_older_server_that_revoked_the_key_is_never_read_live(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    health = tmp_path / "agent_health"
    monkeypatch.setattr(heartbeat_module, "HEALTH_FILE", health)
    server.sends_key_accepted = False
    server.revoked = True
    server.current_key = None
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 8)

    assert agent.credentials.api_key == STALE_KEY
    assert server.recoveries() == []
    assert server.registrations() == []
    assert not health.exists()


# ...while an older server that accepts the key keeps it, sent alone: no
# recovery, no secret after the first answer, live.
async def test_e1_an_older_server_that_accepts_the_key_is_sent_the_key_alone(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    health = tmp_path / "agent_health"
    monkeypatch.setattr(heartbeat_module, "HEALTH_FILE", health)
    server.sends_key_accepted = False
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 6)

    assert server.recoveries() == []
    assert agent.credentials.api_key == STALE_KEY
    assert health.exists()
    later = server.heartbeats()[1:]
    assert all("X-Agent-Recovery-Secret" not in r.headers for r in later)


# ...and a proxy's 401 to the key alone is not the server refusing it.
async def test_e1_a_proxy_401_to_the_key_alone_keeps_the_key(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.sends_key_accepted = False
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _beat_until([agent], lambda: len(server.heartbeats()) >= 2)
    server.forged_401s = 3

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 8)

    assert server.recoveries() == []
    assert agent.credentials.api_key == STALE_KEY
    assert not agent.heartbeat_sender.key_refused


# E3. A save that fails part way (a full disk) left the credentials file
# empty: the old key and the recovery secret were gone, and a restart
# registered a new agent. The file is replaced whole or not at all.
def test_e3_a_failed_save_leaves_the_old_credentials_file_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds_path = tmp_path / "agent_credentials.json"
    agent_id = uuid4()
    creds = AgentCredentials(str(creds_path))
    assert creds.save(agent_id, STALE_KEY, SECRET)

    # A full disk takes a small write into the page cache and refuses it when
    # it is flushed: truncate-then-write empties the file first.
    def full_disk(*_args: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    def truncate_then_full(self: Path, data: bytes) -> int:
        with self.open("wb"):
            pass
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full_disk)
    monkeypatch.setattr(Path, "write_bytes", truncate_then_full)
    assert not creds.save(agent_id, "luxswirl_ak_new")
    monkeypatch.undo()

    reloaded = AgentCredentials(str(creds_path))
    assert reloaded.load()
    assert reloaded.api_key == STALE_KEY
    assert reloaded.recovery_secret == SECRET
    assert [p.name for p in tmp_path.iterdir()] == [creds_path.name]


def _proxy_401_on_first_recovery(srv: Server) -> None:
    """The first recover-key call meets a proxy's 401 page, not the app's."""
    state = {"hit": False}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/recover-key") and not state["hit"]:
            state["hit"] = True
            srv.requests.append(request)
            return httpx.Response(401, text="<html>401 Authorization Required</html>")
        return srv.handle(request)

    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle)
    )


# F1. A proxy's 401 to one recover-key call stranded the agent until restart:
# any 401 there marked recovery refused. Only the server's own refusal does.
async def test_f1_a_proxy_401_on_recover_key_is_asked_again(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(registration_module, "_DEFAULT_RETRY_AFTER", 0.0)
    _proxy_401_on_first_recovery(server)
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: agent.credentials.api_key == server.current_key)

    assert agent.credentials.api_key == server.current_key
    assert len(server.recoveries()) == 2


# ...while the server's own 401 there still stops recovery until restart.
async def test_f1_the_servers_401_on_recover_key_stops_recovery(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.recover_status = 401
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 8)

    assert len(server.recoveries()) == 1
    assert agent.heartbeat_sender.key_refused


def _saved(path: Path, agent_id: UUID) -> AgentCredentials:
    creds = AgentCredentials(str(path))
    assert creds.save(agent_id, STALE_KEY, SECRET)
    return creds


# F2. Where no rename can replace the file (a single-file mount: EBUSY; a
# directory the agent cannot write: EACCES), every save failed. It is
# rewritten in place there instead.
@pytest.mark.parametrize("where", ["rename", "temp file"])
def test_f2_a_file_no_rename_can_replace_is_rewritten_in_place(
    where: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent_credentials.json"
    agent_id = uuid4()
    creds = _saved(path, agent_id)

    def busy(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.EBUSY, "Device or resource busy")

    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.EACCES, "Permission denied")

    if where == "rename":
        monkeypatch.setattr(os, "replace", busy)
    else:
        monkeypatch.setattr(tempfile, "mkstemp", denied)
    assert creds.save(agent_id, "luxswirl_ak_new")
    monkeypatch.undo()

    reloaded = AgentCredentials(str(path))
    assert reloaded.load()
    assert reloaded.api_key == "luxswirl_ak_new"
    assert reloaded.recovery_secret == SECRET
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


# F3. A filesystem that cannot flush a directory (EINVAL): the file was
# already replaced, yet save() reported failure. The save stands.
def test_f3_a_directory_that_cannot_be_flushed_does_not_fail_the_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent_credentials.json"
    agent_id = uuid4()
    creds = _saved(path, agent_id)
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    assert creds.save(agent_id, "luxswirl_ak_new")
    assert creds.api_key == "luxswirl_ak_new"


# F4. A symlinked credentials path: the rename replaced the link with a
# regular file, and the persistent target kept the old key.
def test_f4_a_symlinked_credentials_file_is_written_through(tmp_path: Path) -> None:
    (tmp_path / "persist").mkdir()
    (tmp_path / "data").mkdir()
    target = tmp_path / "persist" / "creds"
    link = tmp_path / "data" / "agent_credentials.json"
    agent_id = uuid4()
    _saved(target, agent_id)
    link.symlink_to(target)

    creds = AgentCredentials(str(link))
    assert creds.load()
    assert creds.save(agent_id, "luxswirl_ak_new")

    assert link.is_symlink()
    reloaded = AgentCredentials(str(target))
    assert reloaded.load()
    assert reloaded.api_key == "luxswirl_ak_new"


# G2 (round 4, owner ruling): the recovery secret rode beside an accepted
# key on every heartbeat, so one captured heartbeat held key and secret. While
# the server accepts the key, the secret never leaves the agent.
async def test_g2_no_heartbeat_with_an_accepted_key_carries_the_secret(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 5)

    assert len(server.heartbeats()) >= 5
    assert all("X-Agent-Recovery-Secret" not in b.headers for b in server.heartbeats())


# ...nor once a regenerated key was recovered: it is sent while the key is
# refused, then never again.
async def test_g2_the_secret_stops_once_the_new_key_is_accepted(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _beat_until([agent], lambda: agent.credentials.api_key == server.current_key)
    server.requests.clear()

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 4)

    assert all("X-Agent-Recovery-Secret" not in b.headers for b in server.heartbeats())
    assert agent.credentials.recovery_secret == SECRET


# G5 (owner ruling): a Revoke voids the recovery secret. The agent, its key
# refused and then its secret, asks recover-key once, is refused there too,
# and registers again as a new pending agent, once, not at every heartbeat.
async def test_g5_after_a_revoke_the_agent_registers_again_once(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.current_key = None
    server.revoked = True
    server.secret_voided = True
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await agent.reporter.connect()
    try:
        await _beat_until([agent], lambda: len(server.registrations()) >= 1)
        await _beat_until([agent], lambda: len(server.heartbeats()) >= 12)

        assert len(server.recoveries()) == 1
        assert len(server.registrations()) == 1
        (register,) = server.registrations()
        assert register.headers["Authorization"] == f"Bearer {REG_KEY}"
        # The new agent's credentials replaced the dead ones.
        reloaded = AgentCredentials(str(tmp_path / "agent_credentials.json"))
        assert reloaded.load()
        assert reloaded.agent_id == server.new_agent_id
        assert reloaded.api_key is None
        assert reloaded.recovery_secret == server.new_secret
        # It now heartbeats as that pending agent, and its reports go out
        # with the registration key, not the revoked one.
        last = server.heartbeats()[-1]
        assert last.headers["Authorization"] == f"Bearer {REG_KEY}"
        assert last.headers["X-Agent-Recovery-Secret"] == server.new_secret
        assert agent.config["agent_id"] == server.new_agent_id
        assert agent.reporter.client is not None
        assert agent.reporter.client.headers["Authorization"] == f"Bearer {REG_KEY}"
    finally:
        await agent.reporter.close()


# ...and a registration that fails (no active registration key) keeps the old
# credentials and is not tried again at every heartbeat.
async def test_g5_a_failed_registration_after_a_revoke_keeps_the_credentials(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.current_key = None
    server.revoked = True
    server.secret_voided = True
    server.register_status = 401
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 12)

    assert len(server.heartbeats()) >= 12
    assert len(server.registrations()) == 1
    assert len(server.recoveries()) == 1
    reloaded = AgentCredentials(str(tmp_path / "agent_credentials.json"))
    assert reloaded.load()
    assert reloaded.agent_id == server.agent_id
    assert reloaded.recovery_secret == SECRET


# ...while a proxy's 401 to a heartbeat that carried the secret neither asks
# recover-key nor registers again.
async def test_g5_a_proxy_401_to_the_secret_registers_nothing(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    server.recover_status = 503  # recovery cannot finish: the secret keeps riding
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _beat_until([agent], lambda: agent.heartbeat_sender.key_refused)
    server.forged_401s = 5

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 10)

    assert server.registrations() == []
    assert agent.credentials.agent_id == server.agent_id


# Round 5 (attacker I #2): a registration that failed after a revoke was
# tried again only on another 401 to the secret, but the real server answers
# 429 once the agent's pending budget is spent (by then, minute 5), so the
# retry waited up to an hour. It is tried on its own wait, whatever the
# heartbeat is answered.
async def test_r5_a_failed_registration_is_retried_past_the_pending_budget(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(registration_module, "_REGISTER_AGAIN_WAIT", 0.2)
    server.current_key = None
    server.revoked = True
    server.secret_voided = True
    server.register_status = 401  # no active registration key, at first
    server.pending_budget = 3
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _beat_until([agent], lambda: len(server.registrations()) >= 1)
    server.register_status = 201  # the operator issues a registration key

    await _beat_until(
        [agent], lambda: agent.credentials.agent_id == server.new_agent_id
    )

    assert agent.credentials.agent_id == server.new_agent_id
    assert len(server.registrations()) == 2
    # recover-key was asked once, not again on each retry.
    assert len(server.recoveries()) == 1


# Round 5, attacker H. H1: a gateway in front of the server (Spring Cloud
# Gateway, an auth sidecar, a second FastAPI service a proxy misroutes to)
# answers a 401 with JSON of its own, which the agent took for the server's
# refusal of its secret, and registered again over valid credentials. Only a
# 401 the server marks (X-LuxSwirl-Refused) makes it register again.
SPRING_401 = {
    "timestamp": "2026-11-04T10:00:00.000+00:00",
    "status": 401,
    "error": "Unauthorized",
    "path": "/api/v1/agent/heartbeat",
}


def _gateway(server: Server, body: dict[str, Any] | None, blocked: list[int]) -> Any:
    """Wrap the fake server: the next `blocked[0]` requests get the gateway's
    401 (JSON `body`, or an HTML page when None), then traffic flows again."""

    def handle(request: httpx.Request) -> httpx.Response:
        if blocked[0] > 0:
            blocked[0] -= 1
            server.requests.append(request)
            if body is None:
                return httpx.Response(
                    401, text="<html>401 Authorization Required</html>"
                )
            return httpx.Response(401, json=body)
        return server.handle(request)

    return handle


async def _install(server: Server, body: dict[str, Any] | None, n: int) -> None:
    await http_client.aclose_all()
    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_gateway(server, body, [n]))
    )


async def _run_blip(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: dict[str, Any] | None,
) -> LuxSwirlAgent:
    # The key is VALID throughout: nothing was regenerated or revoked.
    server.current_key = STALE_KEY
    # The gateway refuses three requests in a row: heartbeat, the next
    # heartbeat (now carrying the secret), and the recover-key it triggers;
    # it is back by the time the agent registers.
    await _install(server, body, 3)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _beat_until([agent], lambda: len(server.heartbeats()) >= 8)
    return agent


async def test_h1_a_gateway_json_401_blip_discards_valid_credentials(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = await _run_blip(server, monkeypatch, tmp_path, SPRING_401)

    reloaded = AgentCredentials(str(tmp_path / "agent_credentials.json"))
    assert reloaded.load()
    # The server never refused the key: the agent must still hold it.
    assert server.registrations() == [], "registered a new agent over a valid key"
    assert reloaded.agent_id == server.agent_id
    assert reloaded.api_key == STALE_KEY
    assert agent.config["auth_key"] == STALE_KEY


async def test_h1_control_an_html_401_blip_keeps_the_credentials(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = await _run_blip(server, monkeypatch, tmp_path, None)

    reloaded = AgentCredentials(str(tmp_path / "agent_credentials.json"))
    assert reloaded.load()
    assert server.registrations() == []
    assert reloaded.agent_id == server.agent_id
    assert reloaded.api_key == STALE_KEY
    assert agent.config["auth_key"] == STALE_KEY


# H2. One gateway JSON 401 against a server from before agent_key_accepted
# (origin/main): the key stays valid, but key_refused never clears unless a
# recovery succeeds. With the registration key revoked (documented as fine
# for an approved agent), the secret rides every heartbeat and the liveness
# file is never stamped again, until a restart.
async def test_h2_older_server_one_json_401_secret_rides_forever(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    health = tmp_path / "agent_health"
    monkeypatch.setattr(heartbeat_module, "HEALTH_FILE", health)
    server.current_key = STALE_KEY
    server.sends_key_accepted = False
    server.recover_status = 401  # registration key revoked
    await _install(server, SPRING_401, 1)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _beat_until([agent], lambda: len(server.heartbeats()) >= 10)

    tail = server.heartbeats()[-5:]
    assert all(b.headers.get("Authorization") == f"Bearer {STALE_KEY}" for b in tail)
    health.unlink(missing_ok=True)
    await _beat_until([agent], lambda: len(server.heartbeats()) >= 14)
    assert health.exists(), "never stamped live although the server takes its key"
    assert all("X-Agent-Recovery-Secret" not in b.headers for b in tail), (
        "secret rides beside a key the server accepts"
    )


async def test_h2_control_older_server_html_401_keeps_secret_home(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    health = tmp_path / "agent_health"
    monkeypatch.setattr(heartbeat_module, "HEALTH_FILE", health)
    server.current_key = STALE_KEY
    server.sends_key_accepted = False
    server.recover_status = 401
    await _install(server, None, 1)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    await _beat_until([agent], lambda: len(server.heartbeats()) >= 10)

    tail = server.heartbeats()[-5:]
    health.unlink(missing_ok=True)
    await _beat_until([agent], lambda: len(server.heartbeats()) >= 14)
    assert health.exists()
    assert all("X-Agent-Recovery-Secret" not in b.headers for b in tail)


# Round 6, attacker J. J2: a gateway that keeps answering JSON 401 got the
# secret on 9 of 10 heartbeats: an unmarked 401 to the secret left the key
# refused. The server never sends one, so the agent stops sending the secret
# and, until a 200, believes only the server's marked refusal of its key.
async def test_j2_a_sustained_gateway_401_gets_the_secret_at_most_once(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.current_key = STALE_KEY  # the key is valid throughout
    await _install(server, SPRING_401, 10)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 12)

    with_secret = [
        b for b in server.heartbeats()[:10] if "X-Agent-Recovery-Secret" in b.headers
    ]
    assert len(with_secret) <= 1, f"secret sent on {len(with_secret)} of 10 beats"
    assert agent.credentials.api_key == STALE_KEY
    assert not agent.heartbeat_sender.key_refused


# ...while the server's marked refusal after such a gateway still starts a
# recovery: a key regenerated meanwhile is recovered.
async def test_j2_after_a_gateway_the_servers_refusal_still_recovers(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    await _install(server, SPRING_401, 2)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: agent.credentials.api_key == server.current_key)

    assert agent.credentials.api_key == server.current_key
    assert len(server.recoveries()) == 1


def _legacy(tmp_path: Path, server: Server) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    AgentCredentials(str(tmp_path / "agent_credentials.json")).save(
        server.agent_id, STALE_KEY
    )


# J3: a legacy agent (a key, no recovery secret) behind a gateway that sends
# one JSON 401 was told, at ERROR, to delete its credentials file, though its
# key works. It says so only on the server's marked refusal (or a run of
# unmarked ones, from an older server that marks none).
async def test_j3_one_gateway_401_never_says_delete_the_credentials(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server.current_key = STALE_KEY
    _legacy(tmp_path, server)
    await _install(server, SPRING_401, 1)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 5)

    assert agent.credentials.api_key == STALE_KEY
    assert not [
        r for r in caplog.records if "delete its credentials file" in r.getMessage()
    ]


async def test_j3_the_servers_refusal_of_a_legacy_key_says_so(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    _legacy(tmp_path, server)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 3)

    assert [
        r for r in caplog.records if "delete its credentials file" in r.getMessage()
    ]


# Round 7, attacker L. The agent inferred the server's intent from unmarked
# 401s on three endpoints. Now every refusal the server sends is marked, and
# an unmarked 401 or 403 is temporary wherever it comes.
def _gateway_on_heartbeat(server: Server, nth: int) -> Any:
    """The `nth` heartbeat gets a gateway's JSON 401; all else reaches the
    (marking) server."""
    seen = [0]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == HEARTBEAT:
            seen[0] += 1
            if seen[0] == nth:
                server.requests.append(request)
                return httpx.Response(401, json=SPRING_401)
        return server.handle(request)

    return handle


def _gateway_on_first_recovery(server: Server) -> Any:
    """The first recover-key gets a gateway's JSON 401, as the server's own
    unmarked registration-key refusal once looked."""
    blocked = [1]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/recover-key") and blocked[0]:
            blocked[0] -= 1
            server.requests.append(request)
            return httpx.Response(401, json=SPRING_401)
        return server.handle(request)

    return handle


async def _through(handler: Any) -> None:
    await http_client.aclose_all()
    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )


# L1: a gateway's JSON 401 to the one heartbeat that carried the secret left
# the agent distrusting every unmarked refusal, and an older server marks
# none, so it never recovered. Now that 401 is temporary: the next heartbeat
# proves the secret again, and the regenerated key is recovered.
async def test_l1_a_gateway_401_on_the_secret_beat_still_recovers(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    await _through(_gateway_on_heartbeat(server, nth=2))
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until(
        [agent], lambda: agent.credentials.api_key == server.current_key, timeout=3.0
    )

    assert agent.credentials.api_key == server.current_key
    assert len(server.recoveries()) == 1
    assert server.registrations() == []


# L3: one gateway JSON 401 on recover-key (the server's registration-key
# refusal was an unmarked JSON 401 too) stopped recovery for the life of the
# process: a regenerated key was never recovered, a revoked agent never
# registered again. Only the server's marked registration-key refusal stops
# recovery now; the gateway's is asked again after a minute.
async def test_l3_a_gateway_401_on_recover_key_still_recovers_a_regenerated_key(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(registration_module, "_DEFAULT_RETRY_AFTER", 0.05)
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    await _through(_gateway_on_first_recovery(server))
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until(
        [agent], lambda: agent.credentials.api_key == server.current_key, timeout=3.0
    )

    assert agent.credentials.api_key == server.current_key
    assert len(server.recoveries()) == 2


async def test_l3_a_gateway_401_on_recover_key_still_registers_after_a_revoke(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(registration_module, "_DEFAULT_RETRY_AFTER", 0.05)
    server.current_key = None
    server.revoked = True
    server.secret_voided = True
    await _through(_gateway_on_first_recovery(server))
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.registrations()) >= 1, timeout=3.0)

    assert len(server.registrations()) == 1


# ...and an unmarked 403 is as temporary as an unmarked 401.
async def test_an_unmarked_403_on_recover_key_is_asked_again(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(registration_module, "_DEFAULT_RETRY_AFTER", 0.05)
    server.current_key = "luxswirl_ak_regenerated_by_operator"
    blocked = [1]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/recover-key") and blocked[0]:
            blocked[0] -= 1
            server.requests.append(request)
            return httpx.Response(403, json={"detail": "Forbidden"})
        return server.handle(request)

    await _through(handle)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until(
        [agent], lambda: agent.credentials.api_key == server.current_key, timeout=3.0
    )

    assert agent.credentials.api_key == server.current_key


# L2: after a revoke the server took the new registration (201) but the
# credentials file could not be written; the agent registered AGAIN every
# _REGISTER_AGAIN_WAIT, and the server created another pending agent each
# time. The accepted registration is used from memory, and saved once the
# disk takes it.
def _disk_full(*_args: Any, **_kwargs: Any) -> None:
    raise OSError(errno.ENOSPC, "No space left on device")


async def test_l2_an_unsaveable_registration_registers_once(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(registration_module, "_REGISTER_AGAIN_WAIT", 0.05)
    server.current_key = None
    server.revoked = True
    server.secret_voided = True
    agent = _agent(monkeypatch, tmp_path, server.agent_id)
    write = agent.credentials._write_atomically
    monkeypatch.setattr(agent.credentials, "_write_atomically", _disk_full)

    await _beat_until([agent], lambda: len(server.heartbeats()) >= 10, timeout=3.0)
    assert len(server.registrations()) == 1
    assert agent.credentials.unsaved
    assert agent.config["agent_id"] == server.new_agent_id

    monkeypatch.setattr(agent.credentials, "_write_atomically", write)
    await _beat_until([agent], lambda: not agent.credentials.unsaved, timeout=3.0)

    reloaded = AgentCredentials(str(tmp_path / "agent_credentials.json"))
    assert reloaded.load()
    assert reloaded.agent_id == server.new_agent_id
    assert reloaded.recovery_secret == server.new_secret
    assert reloaded.api_key is None
    assert len(server.registrations()) == 1


async def test_l2_control_a_refused_registration_is_tried_again(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The retry is right when the server created nothing.
    monkeypatch.setattr(registration_module, "_REGISTER_AGAIN_WAIT", 0.05)
    server.current_key = None
    server.revoked = True
    server.secret_voided = True
    server.register_status = 401
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.registrations()) >= 2, timeout=3.0)

    assert len(server.registrations()) >= 2
    assert agent.credentials.agent_id == server.agent_id


# Round 8, N1: the server marked a revoked agent's key, sent alone, as a
# refused registration key (a revoke clears the key hash), and the agent acted
# only on `agent-key`: it never proved its secret, never learned a revoke
# voided it, and never registered again. A key sent alone and refused is the
# agent's key refused, whichever key the server names.
async def test_n1_a_key_alone_refused_as_a_registration_key_still_registers_again(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.current_key = None
    server.revoked = True
    server.secret_voided = True
    answer = server._answer

    def named_registration_key(request: httpx.Request) -> httpx.Response:
        response = answer(request)
        if response.headers.get(REFUSAL_HEADER) == AGENT_KEY_REFUSED:
            return httpx.Response(401, json={"detail": "no"}, headers=MARK_REG)
        return response

    monkeypatch.setattr(server, "_answer", named_registration_key)
    agent = _agent(monkeypatch, tmp_path, server.agent_id)

    await _beat_until([agent], lambda: len(server.registrations()) >= 1, timeout=3.0)

    assert len(server.registrations()) == 1
