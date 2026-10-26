"""The server's agent protocol: every URL the agent calls.

`server_url` (env `LUXSWIRL_SERVER_URL`) is the server's address: scheme, host
and port, no path. The agent protocol lives under `/api/v1/agent` on it; each
endpoint below is the one place its path is spelled.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.shared.url_security import validate_server_url

AGENT_API = "/api/v1/agent"


def server_origin(url: str) -> str:
    """`url` as the server's origin, once it is a safe server address with no path.

    Raises:
        ValueError: when `url` is missing; carries credentials (httpx would send
            them as `Authorization: Basic`, in place of the agent's key); has a
            port that is not 1-65535; carries a path, query or fragment; or is
            plain HTTP to an external host (`validate_server_url`).
    """
    if not url:
        raise ValueError("No server URL: set LUXSWIRL_SERVER_URL")
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "LUXSWIRL_SERVER_URL must not carry credentials (user@ or user:password@)"
        )
    try:
        port = parsed.port
    except ValueError:
        port = 0
    if port == 0:
        raise ValueError(f"LUXSWIRL_SERVER_URL has an invalid port: {url}")
    validate_server_url(url)
    if parsed.path.rstrip("/") or parsed.query or parsed.fragment:
        raise ValueError(
            "LUXSWIRL_SERVER_URL is the server's address with no path "
            f"(e.g. https://luxswirl.example.com:9000), got {url}"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _url(config: dict[str, Any], path: str) -> str:
    return f"{config['server_url']}{AGENT_API}{path}"


def register_url(config: dict[str, Any]) -> str:
    return _url(config, "/register")


def recover_key_url(config: dict[str, Any], agent_id: str) -> str:
    return _url(config, f"/{agent_id}/recover-key")


def heartbeat_url(config: dict[str, Any]) -> str:
    return _url(config, "/heartbeat")


def checks_url(config: dict[str, Any]) -> str:
    return _url(config, "/checks")


def reports_url(config: dict[str, Any]) -> str:
    return _url(config, "/reports")


def job_url(config: dict[str, Any], job_id: str, action: str) -> str:
    """The URL for `action` (start, results) on one of this agent's jobs."""
    return _url(config, f"/jobs/{job_id}/{action}")


def artifacts_url(config: dict[str, Any]) -> str:
    return _url(config, "/artifacts")
