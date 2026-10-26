"""Every JSON route lives under its surface's prefix.

The JSON API has one prefix per caller, each with its own credential:
`/api/v1/agent` is the agent protocol (registration key or agent key), and
`/api/v1/admin` the admin API (a user's API token). A route
mounted anywhere else under `/api` belongs to no surface, so no authz row
(`[[target.authz.signature]]` in .luxarch.toml) and no reader of the API knows
which credential guards it.
"""

from __future__ import annotations

from _route_table import iter_routes
from app.main import app

SURFACES = ("/api/v1/agent/", "/api/v1/admin/")

# The agent protocol: exactly what the agent calls (apps/agent/app/agent/server_api.py).
AGENT_PROTOCOL = {
    ("POST", "/api/v1/agent/register"),
    ("POST", "/api/v1/agent/{agent_id}/recover-key"),
    ("POST", "/api/v1/agent/heartbeat"),
    ("GET", "/api/v1/agent/checks"),
    ("POST", "/api/v1/agent/reports"),
    ("POST", "/api/v1/agent/jobs/{job_id}/start"),
    ("POST", "/api/v1/agent/jobs/{job_id}/results"),
    ("POST", "/api/v1/agent/artifacts"),
}


def _api_routes() -> set[tuple[str, str]]:
    return {
        (method, path)
        for route, path in iter_routes(app.routes)
        if path.startswith("/api/")
        for method in (getattr(route, "methods", None) or ())
        if method != "HEAD"
    }


def test_every_api_route_is_under_a_surface() -> None:
    stray = sorted(
        (method, path)
        for method, path in _api_routes()
        if not path.startswith(SURFACES)
    )
    assert stray == []


def test_the_agent_protocol_is_exactly_what_the_agent_calls() -> None:
    agent = {r for r in _api_routes() if r[1].startswith("/api/v1/agent/")}
    assert agent == AGENT_PROTOCOL
