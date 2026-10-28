"""Process-lifetime HTTP clients for the agent.

Building an `httpx.AsyncClient` per call pays DNS + TCP + TLS setup on every request
and defeats connection pooling entirely — at a few hundred checks a minute that is the
agent's single largest avoidable cost. Clients here are opened once and reused for the
process lifetime (collector.no_per_request_http).

Two surfaces:

- `server_client()` — talks to the LuxSwirl server (registration, artifact upload,
  config fetch). One client, default TLS verification.
- `check_client(verify=...)` — talks to arbitrary CHECK TARGETS, each connection
  on an address the SSRF policy allows (`checks._ssrf_http.VettedTransport`). `verify` is a
  transport-level setting, so it cannot be varied per request; clients are therefore
  pooled per distinct `verify` value (in practice two). Timeouts ARE per-request and
  must be passed at the call site, never baked into the client.

`aclose_all()` is called from the agent's shutdown path.
"""

from __future__ import annotations

import httpx

from app.checks._ssrf_http import VettedTransport

_server_client: httpx.AsyncClient | None = None
_check_clients: dict[bool, httpx.AsyncClient] = {}


def server_client() -> httpx.AsyncClient:
    """Return the shared client for server-facing calls (created on first use)."""
    global _server_client
    if _server_client is None:
        _server_client = httpx.AsyncClient(timeout=30.0)
    return _server_client


def check_client(*, verify: bool = True) -> httpx.AsyncClient:
    """Return the shared client for check targets with this TLS-verification setting.

    Pass the per-check timeout on the REQUEST, not here — a client-level timeout would
    apply the first check's value to every other check sharing the connection pool.
    """
    client = _check_clients.get(verify)
    if client is None:
        # Every connection on an address the SSRF policy allows.
        client = httpx.AsyncClient(transport=VettedTransport(verify=verify))
        _check_clients[verify] = client
    return client


async def aclose_all() -> None:
    """Close every pooled client. Safe to call when nothing was ever opened."""
    global _server_client
    if _server_client is not None:
        await _server_client.aclose()
        _server_client = None
    for client in list(_check_clients.values()):
        await client.aclose()
    _check_clients.clear()
