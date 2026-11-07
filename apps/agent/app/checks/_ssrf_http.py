"""SSRF-guarded httpx send and transport, shared by the http and json checks.

The connections are `shared.ssrf_http`'s, on the address the check's SSRF policy
allows. A pooled connection is reused only under the policy it was vetted under:
`VettedTransport` starts a new pool when the policy changes.

httpx's own ``follow_redirects`` never re-checks a hop, so redirects are followed
here, one request at a time: each hop to a new host opens a new connection, and
that connection is vetted like the first.
"""

from typing import Any

import httpcore
import httpx

from app.checks._ssrf_policy import allowed_addresses, block_private_networks
from app.shared import ssrf_http


class VettedTransport(ssrf_http.VettedTransport):
    """The shared transport under the policy the server sets. A pooled connection
    was vetted under the policy of its pool: when the policy changes, the next
    request starts a new pool, and the old one (whose in-flight requests finish on
    it) is closed with the transport.
    """

    def __init__(self, *, verify: bool) -> None:
        self._retired: list[httpcore.AsyncConnectionPool] = []
        self._policy = block_private_networks()
        super().__init__(verify=verify, allowed=allowed_addresses)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        policy = block_private_networks()
        if policy != self._policy:
            self._retired.append(self._pool)
            self._pool = self._new_pool()
            self._policy = policy
        return await super().handle_async_request(request)

    async def aclose(self) -> None:
        for pool in self._retired:
            await pool.aclose()
        self._retired.clear()
        await super().aclose()


async def ssrf_guarded_send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, Any],
    body: Any = None,
    follow_redirects: bool,
    timeout: float,
    max_redirects: int = 20,
) -> httpx.Response:
    """Send ``method url`` through ``client`` (a `VettedTransport` client),
    following redirects one hop at a time. Raises ``shared.ssrf.SsrfBlockedError``
    if a hop's host has no allowed address (the http/json checks surface that as
    a failure).

    ``timeout`` is per-REQUEST on purpose: ``client`` is shared across checks (pooled by
    TLS-verify setting), so a client-level timeout would apply one check's value to
    every other check using the same pool."""
    content = None if method.upper() in ("GET", "HEAD", "OPTIONS") else body
    request = client.build_request(
        method, url, headers=headers, content=content, timeout=timeout
    )
    limit = max_redirects if follow_redirects else 0
    for _ in range(limit):
        response = await client.send(request, follow_redirects=False)
        if not response.is_redirect or response.next_request is None:
            return response
        request = response.next_request
        request.extensions["timeout"] = httpx.Timeout(timeout).as_dict()
    # Redirects disabled or limit reached — issue the final hop.
    return await client.send(request, follow_redirects=False)
