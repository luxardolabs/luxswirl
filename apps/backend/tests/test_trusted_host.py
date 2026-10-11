"""The server refuses a Host header it does not serve.

Anything built from the request's Host (a redirect, an absolute URL in a page or
an email) points wherever the client says, so the app itself checks Host against
the required ALLOWED_HOSTS setting. These requests go through the real app, so a
middleware that was not installed, or installed under a condition, fails here.
"""

from __future__ import annotations

import pytest
from app.core.config import Settings, settings
from app.core.trusted_host import AllowedHostsError, parse_allowed_hosts
from app.main import app
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

pytestmark = pytest.mark.integration

ALLOWED = settings.allowed_hosts[0]


def _client(peer: str = "127.0.0.1") -> AsyncClient:
    """An ASGI client whose TCP peer is ``peer`` (httpx's default is loopback)."""
    transport = ASGITransport(app=app, client=(peer, 50000))
    return AsyncClient(transport=transport, base_url=f"http://{ALLOWED}")


async def test_allowed_host_is_served() -> None:
    async with _client() as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


@pytest.mark.parametrize(
    "path", ["/health", "/", "/login", "/api/v1/agents", "/metrics"]
)
async def test_forged_host_is_refused_on_every_route(path: str) -> None:
    async with _client(peer="10.0.0.7") as c:
        r = await c.get(path, headers={"Host": "attacker.example"})
    assert r.status_code == 400


async def test_forged_host_from_loopback_is_refused_off_the_health_path() -> None:
    # A loopback peer earns no exemption on its own: only the in-container probe shape does.
    async with _client() as c:
        r = await c.get("/login", headers={"Host": "attacker.example"})
    assert r.status_code == 400


async def test_localhost_host_is_refused_from_a_proxied_peer() -> None:
    # `Host: localhost` through nginx arrives from the proxy's address, not loopback.
    async with _client(peer="172.18.0.5") as c:
        r = await c.get("/health", headers={"Host": "localhost"})
    assert r.status_code == 400


async def test_container_healthcheck_passes_without_localhost_listed() -> None:
    # compose's HEALTHCHECK: requests.get('http://localhost:9000/health') inside the container.
    assert "localhost" not in settings.allowed_hosts
    async with _client() as c:
        r = await c.get("/health", headers={"Host": "localhost:9000"})
    assert r.status_code == 200


async def test_in_container_probe_does_not_reach_other_routes() -> None:
    async with _client() as c:
        r = await c.get("/login", headers={"Host": "localhost:9000"})
    assert r.status_code == 400


@pytest.mark.parametrize("value", ["", " , ", "*", "example.com,*"])
def test_unusable_allowed_hosts_refuse(value: str) -> None:
    with pytest.raises(AllowedHostsError):
        parse_allowed_hosts(value)


def test_allowed_hosts_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    with pytest.raises(ValidationError, match="allowed_hosts"):
        Settings(_env_file=None)


def test_allowed_hosts_reads_a_comma_separated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOWED_HOSTS", "swirl.example.com, luxswirl_server")
    assert Settings(_env_file=None).allowed_hosts == [
        "swirl.example.com",
        "luxswirl_server",
    ]
