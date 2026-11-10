"""An httpx transport whose connections open only on addresses an SSRF policy allows.

Every connection goes through `VettedBackend`: it resolves the host once, keeps the
addresses the policy allows, and opens the socket on the first of them that answers,
so nothing resolves the name again between the check and the connection (DNS
rebinding). TLS and the Host header still use the name.

A client on this transport connects directly: a proxy from the environment
(HTTP_PROXY and the like) would resolve the name itself, out of the policy's reach,
and httpx reads no proxy from the environment for a client given a transport.
"""

import asyncio
import contextvars
import ssl
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import Executor
from typing import Any

import anyio
import httpcore
import httpx

AllowedAddresses = Callable[[str], list[str]]
# The same, as a coroutine that runs the lookup where it chooses.
AllowedAddressesLookup = Callable[[str], Awaitable[list[str]]]

# How a `VettedBackend`'s ConnectError opens when the host has no address.
NO_ADDRESS = "Could not resolve host"


class _ClosedOnCancelStream(httpcore.AsyncNetworkStream):
    """A connection that is closed when a cancel lands in its TLS handshake. httpcore
    closes it there on an Exception only, and a cancel is none, so the socket would stay
    open for as long as the target held the handshake."""

    def __init__(self, stream: httpcore.AsyncNetworkStream) -> None:
        self._stream = stream

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            return await self._stream.start_tls(ssl_context, server_hostname, timeout)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await self._stream.aclose()
            raise

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class VettedBackend(httpcore.AsyncNetworkBackend):
    """Opens each TCP connection on an address `allowed` returns for its host.

    Raises what `allowed` raises when it refuses the host (`shared.ssrf.SsrfBlockedError`)
    and ``httpcore.ConnectError`` when the host has no address. A lookup is
    `lookup`, awaited, when one is given; else `allowed` runs on `lookups`, or on the
    loop's default executor. Each address but the last gets at most
    `address_seconds`, so a dead one leaves time for the next.
    """

    def __init__(
        self,
        allowed: AllowedAddresses,
        lookups: Executor | None = None,
        address_seconds: float | None = None,
        lookup: AllowedAddressesLookup | None = None,
    ) -> None:
        self._allowed = allowed
        self._lookups = lookups
        self._address_seconds = address_seconds
        self._lookup = lookup
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if self._lookup is not None:
            addresses = await self._lookup(host)
        else:
            addresses = await asyncio.get_running_loop().run_in_executor(
                self._lookups, contextvars.copy_context().run, self._allowed, host
            )
        last: Exception = httpcore.ConnectError(f"{NO_ADDRESS}: {host}")
        # Each allowed address in the resolver's order, as a client given the
        # name would: a dual-stack host with a dead IPv6 route answers on IPv4.
        for address in addresses:
            budget = timeout
            if self._address_seconds is not None and address != addresses[-1]:
                budget = min(timeout or self._address_seconds, self._address_seconds)
            try:
                stream = await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=budget,
                    local_address=local_address,
                    socket_options=socket_options,
                )
                return _ClosedOnCancelStream(stream)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("A vetted connection is over TCP only")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class VettedTransport(httpx.AsyncHTTPTransport):
    """httpx's transport with its connection pool on `VettedBackend`.

    httpx takes no network backend, so the pool it builds is replaced by the same
    pool with one.
    """

    def __init__(
        self,
        *,
        verify: bool,
        allowed: AllowedAddresses,
        lookups: Executor | None = None,
        address_seconds: float | None = None,
        lookup: AllowedAddressesLookup | None = None,
    ) -> None:
        super().__init__(verify=verify)
        self._verify = verify
        self._allowed = allowed
        self._lookups = lookups
        self._address_seconds = address_seconds
        self._lookup = lookup
        self._pool = self._new_pool()

    def _new_pool(self) -> httpcore.AsyncConnectionPool:
        return httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=self._verify),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=VettedBackend(
                self._allowed, self._lookups, self._address_seconds, self._lookup
            ),
        )
