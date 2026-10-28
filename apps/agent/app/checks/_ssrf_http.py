"""SSRF-guarded httpx send and transport, shared by the http and json checks.

Every connection a check client opens goes through `VettedBackend`: it resolves
the host once, keeps the addresses the SSRF policy allows, and opens the socket on
the first of them that answers, so nothing resolves the name again between the
check and the connection (DNS rebinding). TLS and the Host header still use the
name. A pooled connection is reused only under the policy it was vetted under:
`VettedTransport` starts a new pool when the policy changes.

Check traffic connects directly: a proxy from the environment (HTTP_PROXY and the
like) would resolve the name itself, out of the policy's reach, so the check
clients take none.

httpx's own ``follow_redirects`` never re-checks a hop, so redirects are followed
here, one request at a time: each hop to a new host opens a new connection, and
that connection is vetted like the first.
"""

import asyncio
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx

from app.checks._ssrf_policy import allowed_addresses, block_private_networks


class VettedBackend(httpcore.AsyncNetworkBackend):
    """Opens each TCP connection on an address the SSRF policy allows.

    Raises ``shared.ssrf.SsrfBlockedError`` when the host resolves and every
    address is blocked, and ``httpcore.ConnectError`` when it does not resolve.
    """

    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await asyncio.to_thread(allowed_addresses, host)
        last: Exception = httpcore.ConnectError(f"Could not resolve host: {host}")
        # Each allowed address in the resolver's order, as a client given the
        # name would: a dual-stack host with a dead IPv6 route answers on IPv4.
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("A check connects over TCP only")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class VettedTransport(httpx.AsyncHTTPTransport):
    """httpx's transport with its connection pool on `VettedBackend`.

    httpx takes no network backend, so the pool it builds is replaced by the
    same pool with one. A pooled connection was vetted under the policy of its
    pool: when the policy changes, the next request starts a new pool, and the
    old one (whose in-flight requests finish on it) is closed with the transport.
    """

    def __init__(self, *, verify: bool) -> None:
        super().__init__(verify=verify)
        self._verify = verify
        self._retired: list[httpcore.AsyncConnectionPool] = []
        self._policy = block_private_networks()
        self._pool = self._new_pool()

    def _new_pool(self) -> httpcore.AsyncConnectionPool:
        return httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=self._verify),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=VettedBackend(),
        )

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
