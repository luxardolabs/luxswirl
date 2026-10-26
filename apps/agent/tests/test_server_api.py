"""The agent reaches the server only at `server_url` + the agent protocol's paths.

`server_url` is the server's address with no path; a URL that carries one
(`.../api/v1/reports`) would put every request on a path the server does not
serve, so the agent refuses it at startup.
"""

from __future__ import annotations

import pytest
from app.agent import server_api
from app.agent.core import LuxSwirlAgent

pytestmark = pytest.mark.unit

CONFIG = {"server_url": "https://server.example:9000"}


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://server.example:9000", "https://server.example:9000"),
        ("https://server.example:9000/", "https://server.example:9000"),
        ("http://luxswirl_server:9000", "http://luxswirl_server:9000"),
    ],
)
def test_a_server_address_is_its_origin(url: str, origin: str) -> None:
    assert server_api.server_origin(url) == origin


@pytest.mark.parametrize(
    "url",
    [
        "https://server.example:9000/api/v1/reports",
        "https://server.example:9000/luxswirl",
        "https://server.example:9000?x=1",
        "https://server.example:9000#top",
    ],
)
def test_a_server_address_with_a_path_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="LUXSWIRL_SERVER_URL"):
        server_api.server_origin(url)


@pytest.mark.parametrize(
    "url",
    ["https://user:pw@server.example:9000", "https://agent@server.example"],
)
def test_a_server_address_with_credentials_is_refused(url: str) -> None:
    # httpx sends userinfo as `Authorization: Basic`, replacing the agent's key.
    with pytest.raises(ValueError, match="credentials"):
        server_api.server_origin(url)


@pytest.mark.parametrize(
    "url", ["http://localhost:99999", "http://localhost:abc", "http://localhost:0"]
)
def test_a_server_address_with_a_bad_port_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="port"):
        server_api.server_origin(url)


@pytest.mark.parametrize("url", ["", "http://monitor.example.com:9000"])
def test_a_missing_or_insecure_server_address_is_refused(url: str) -> None:
    with pytest.raises(ValueError):
        server_api.server_origin(url)


def test_every_endpoint_is_under_the_agent_protocol() -> None:
    base = "https://server.example:9000/api/v1/agent"
    assert {
        server_api.register_url(CONFIG),
        server_api.recover_key_url(CONFIG, "A1"),
        server_api.heartbeat_url(CONFIG),
        server_api.checks_url(CONFIG),
        server_api.reports_url(CONFIG),
        server_api.job_url(CONFIG, "J1", "start"),
        server_api.job_url(CONFIG, "J1", "results"),
        server_api.artifacts_url(CONFIG),
    } == {
        f"{base}/register",
        f"{base}/A1/recover-key",
        f"{base}/heartbeat",
        f"{base}/checks",
        f"{base}/reports",
        f"{base}/jobs/J1/start",
        f"{base}/jobs/J1/results",
        f"{base}/artifacts",
    }


def test_the_agent_will_not_start_on_a_server_address_with_a_path() -> None:
    with pytest.raises(ValueError, match="no path"):
        LuxSwirlAgent({"server_url": "http://luxswirl_server:9000/api/v1/reports"})
