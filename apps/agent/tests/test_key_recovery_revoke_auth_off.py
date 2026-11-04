"""Round 9, P1: a revoked agent whose server runs with
authentication off. The fake answers exactly what the real server answers
(checked against the real app by the attacker of round 9):

- heartbeat (key alone, or key + secret): 200, approval_status active,
  agent_key_accepted false (no key hash left, the pending path lets it in)
- recover-key: 401 marked recovery-secret (core checks the voided secret
  whatever auth_enabled says)
- reports: 403 (no key hash)

Spec: a Revoke voids key and secret; the install registers again as a new
pending agent. It never did: it asked recover-key at every heartbeat,
forever. recover-key refusing the secret as void now registers it again.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import test_key_recovery_attacks as base
from app.agent import http_client
from app.shared.agent_protocol import (
    AGENT_KEY_REFUSED,
    RECOVERY_SECRET_REFUSED,
    REFUSAL_HEADER,
)

pytestmark = pytest.mark.unit

NEW_ID = uuid4()
NEW_SECRET = "luxswirl_rs_" + "ef" * 32


class RevokedServer:
    def __init__(self, *, auth_enabled: bool) -> None:
        self.auth_enabled = auth_enabled
        self.agent_id = uuid4()
        self.requests: list[httpx.Request] = []
        self.registered = False

    def of(self, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(suffix)]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        secret = request.headers.get("X-Agent-Recovery-Secret")
        if path == base.HEARTBEAT:
            if secret == NEW_SECRET:
                return httpx.Response(200, json={"approval_status": "pending"})
            if self.auth_enabled:
                if secret is None:
                    return httpx.Response(
                        401,
                        json={"detail": "x"},
                        headers={REFUSAL_HEADER: AGENT_KEY_REFUSED},
                    )
                return httpx.Response(
                    401,
                    json={"detail": "x"},
                    headers={REFUSAL_HEADER: RECOVERY_SECRET_REFUSED},
                )
            return httpx.Response(
                200,
                json={
                    "approval_status": "active",
                    "agent_key_accepted": False,
                    "config_version": "v1",
                },
            )
        if path.endswith("/recover-key"):
            return httpx.Response(
                401,
                json={"error": "AUTHENTICATION_FAILED", "message": "no"},
                headers={REFUSAL_HEADER: RECOVERY_SECRET_REFUSED},
            )
        if path == base.REGISTER:
            self.registered = True
            return httpx.Response(
                201,
                json={
                    "agent_id": str(NEW_ID),
                    "recovery_secret": NEW_SECRET,
                    "status": "pending",
                },
            )
        if path == base.CHECKS:
            return httpx.Response(
                401, json={"detail": "no"}, headers={REFUSAL_HEADER: "refused"}
            )
        return httpx.Response(404)


async def _server(auth_enabled: bool) -> RevokedServer:
    fake = RevokedServer(auth_enabled=auth_enabled)
    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handle)
    )
    return fake


@pytest_asyncio.fixture
async def cleanup() -> AsyncIterator[None]:
    yield
    await http_client.aclose_all()


@pytest.mark.parametrize(
    "auth_enabled", [False, True], ids=["auth_off", "control_auth_on"]
)
async def test_p1_revoked_agent_registers_again(
    cleanup: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    auth_enabled: bool,
) -> None:
    fake = await _server(auth_enabled)
    agent = base._agent(monkeypatch, tmp_path, fake.agent_id)

    await base._beat_until(
        [agent], lambda: fake.registered or len(fake.of(base.HEARTBEAT)) >= 15
    )

    assert fake.registered, (
        f"never registered again: {len(fake.of(base.HEARTBEAT))} heartbeats, "
        f"{len(fake.of('/recover-key'))} recover-key calls"
    )
    assert agent.config["agent_id"] == NEW_ID
