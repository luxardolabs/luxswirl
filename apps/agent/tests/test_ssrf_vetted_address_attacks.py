"""Checks connecting to vetted addresses, against what an attacker or a network
throws at them: the HTTPS certificate probe, a policy change under a pooled
connection, an allowed address that does not answer, an environment proxy, a
name the agent cannot resolve, and IPv6 on macOS.

Controls (`test_control_*`) show the harness works. Every socket and subprocess
wait is bounded (5 s); servers are closed without waiting on their clients.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import ipaddress
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpcore
import httpx
import pytest
from app.agent import http_client
from app.checks import _ssrf_policy
from app.checks._ssrf_http import ssrf_guarded_send
from app.checks.http import HTTPCheck
from app.checks.ping import PingCheck
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

pytestmark = pytest.mark.unit

METADATA = "169.254.169.254"


async def _b[T](aw: Awaitable[T]) -> T:
    return await asyncio.wait_for(aw, 5)


@contextlib.asynccontextmanager
async def _closing(server: asyncio.Server) -> AsyncIterator[None]:
    try:
        yield
    finally:
        server.close()
        with contextlib.suppress(Exception):
            server.close_clients()


def _infos(*ips: str, port: int = 0) -> list[tuple[Any, ...]]:
    out = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        address = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
        out.append((family, socket.SOCK_STREAM, 6, "", address))
    return out


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    real = socket.getaddrinfo

    def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name in table:
            return _infos(*table[name], port=int(port or 0))
        return real(host, port, *args, **kwargs)

    async def fake_async(
        self: Any, host: Any, port: Any, *args: Any, **kwargs: Any
    ) -> Any:
        return fake(host, port)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_async)
    return table


@pytest.fixture(autouse=True)
async def fresh_clients() -> Any:
    _ssrf_policy._policy["block_private_networks"] = False
    yield
    _ssrf_policy._policy["block_private_networks"] = False
    with contextlib.suppress(Exception):
        await _b(http_client.aclose_all())


def _cfg(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "check_id": str(uuid4()),
        "name": "attack",
        "check_type": "http",
        "retries": 1,
        "timeout": 2,
    }
    cfg.update(overrides)
    return cfg


def _tls_context(tmp_path: Path) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "internal-secret-host")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "c.pem"
    key_file = tmp_path / "k.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


async def _ok_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        await _b(reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        await _b(writer.drain())
    except Exception:
        pass
    writer.close()


def _spy_create_connection(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record every (host asked, address tried) of socket.create_connection, in
    resolver order like the real one; only loopback is actually opened, a
    non-loopback attempt is recorded and treated as failed (no network)."""
    attempts: list[tuple[str, str]] = []
    real = socket.create_connection

    def spy(address: tuple[str, int], timeout: Any = None, *a: Any, **k: Any) -> Any:
        host, port = address
        for info in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
            ip = info[4][0]
            attempts.append((host, ip))
            if ipaddress.ip_address(ip).is_loopback:
                return real((ip, port), 3)
        raise OSError("no address reachable")

    monkeypatch.setattr(socket, "create_connection", spy)
    return attempts


# ── 1. The HTTPS certificate probe resolves the name itself ─────────────────


async def test_cert_probe_never_connects_to_a_blocked_address(
    answers, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = await asyncio.start_server(
        _ok_handler, "127.0.0.1", 0, ssl=_tls_context(tmp_path)
    )
    port = server.sockets[0].getsockname()[1]
    answers["mixed.test"] = [METADATA, "127.0.0.1"]
    attempts = _spy_create_connection(monkeypatch)
    async with _closing(server):
        result = await _b(
            HTTPCheck(
                _cfg(target=f"https://mixed.test:{port}/", verify_ssl=False)
            ).run()
        )
    assert result["success"] is True, result
    tried = [ip for _, ip in attempts]
    assert METADATA not in tried, (
        f"cert probe went toward a blocked address: {attempts}"
    )


async def test_control_cert_probe_single_allowed_answer(
    answers, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = await asyncio.start_server(
        _ok_handler, "127.0.0.1", 0, ssl=_tls_context(tmp_path)
    )
    port = server.sockets[0].getsockname()[1]
    answers["one.test"] = ["127.0.0.1"]
    attempts = _spy_create_connection(monkeypatch)
    async with _closing(server):
        result = await _b(
            HTTPCheck(_cfg(target=f"https://one.test:{port}/", verify_ssl=False)).run()
        )
    assert result["success"] is True, result
    assert [ip for _, ip in attempts] == ["127.0.0.1"]


async def test_cert_probe_reads_a_private_cert_with_private_blocked(
    answers, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rebinding on the probe: the vetted fetch sees a public answer, the probe's
    own resolve sees a private one; the private cert lands in the result."""
    server = await asyncio.start_server(
        _ok_handler, "127.0.0.1", 0, ssl=_tls_context(tmp_path)
    )
    port = server.sockets[0].getsockname()[1]
    _ssrf_policy._policy["block_private_networks"] = True
    real_connect = httpcore.AnyIOBackend.connect_tcp

    async def to_local(self: Any, host: str, p: int, *a: Any, **k: Any) -> Any:
        assert host == "203.0.113.7", host  # the vetted public address
        return await real_connect(self, "127.0.0.1", p, *a, **k)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", to_local)
    answers["rebind.test"] = ["203.0.113.7"]
    attempts = _spy_create_connection(monkeypatch)
    spy = socket.create_connection

    def rebind_then_spy(address: tuple[str, int], *a: Any, **k: Any) -> Any:
        answers["rebind.test"] = ["127.0.0.1"]  # TTL 0: next answer is private
        return spy(address, *a, **k)

    monkeypatch.setattr(socket, "create_connection", rebind_then_spy)
    async with _closing(server):
        result = await _b(
            HTTPCheck(
                _cfg(target=f"https://rebind.test:{port}/", verify_ssl=False)
            ).run()
        )
    assert result["success"] is True, result
    cert = result.get("metrics", {}).get("response", {}).get("ssl_certificate")
    assert not cert, f"private cert read with private blocked: {cert} ({attempts})"


# ── 2. Keep-alive connection reused across a policy change ──────────────────


async def test_pooled_connection_not_reused_after_private_blocked(answers) -> None:
    async def keepalive(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            for _ in range(5):
                await _b(reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await _b(writer.drain())
        except Exception:
            pass
        writer.close()

    server = await asyncio.start_server(keepalive, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    answers["lan.test"] = ["127.0.0.1"]
    client = http_client.check_client(verify=True)
    url = f"http://lan.test:{port}/"
    kw: dict[str, Any] = {
        "headers": {},
        "body": None,
        "follow_redirects": True,
        "timeout": 2,
    }
    second: Any = None
    async with _closing(server):
        first = await _b(ssrf_guarded_send(client, "GET", url, **kw))
        assert first.status_code == 200
        _ssrf_policy.apply_server_policy({"block_private_networks": True})
        with contextlib.suppress(Exception):
            second = await _b(ssrf_guarded_send(client, "GET", url, **kw))
    assert second is None, (
        f"now-blocked private host answered on a pooled connection: {second}"
    )


async def test_control_new_connection_refused_after_private_blocked(answers) -> None:
    server = await asyncio.start_server(_ok_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    answers["lan.test"] = ["127.0.0.1"]
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    client = http_client.check_client(verify=True)
    async with _closing(server):
        with pytest.raises(ValueError, match="private"):
            await _b(
                ssrf_guarded_send(
                    client,
                    "GET",
                    f"http://lan.test:{port}/",
                    headers={},
                    body=None,
                    follow_redirects=True,
                    timeout=2,
                )
            )


# ── 3. No fallback to the next allowed address ──────────────────────────────


async def test_http_falls_back_to_the_next_allowed_address(answers) -> None:
    server = await asyncio.start_server(_ok_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    answers["dual.test"] = ["::1", "127.0.0.1"]  # nothing listens on ::1
    async with _closing(server):
        result = await _b(HTTPCheck(_cfg(target=f"http://dual.test:{port}/")).run())
    assert result["success"] is True, result


async def test_control_plain_httpx_falls_back(answers) -> None:
    server = await asyncio.start_server(_ok_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    answers["dual.test"] = ["::1", "127.0.0.1"]
    async with _closing(server), httpx.AsyncClient() as client:
        response = await _b(client.get(f"http://dual.test:{port}/"))
    assert response.status_code == 200


# ── 4. Environment proxies are silently dropped ─────────────────────────────


async def _proxy() -> tuple[asyncio.Server, int, list[bytes]]:
    seen: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with contextlib.suppress(Exception):
            seen.append(await _b(reader.readuntil(b"\r\n\r\n")))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )
            await _b(writer.drain())
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], seen


@pytest.fixture
def env_proxy(monkeypatch: pytest.MonkeyPatch) -> Any:
    for var in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)

    def set_proxy(port: int) -> None:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{port}")

    return set_proxy


async def test_check_client_takes_no_proxy_from_the_environment(env_proxy) -> None:
    """A proxy would resolve the name itself, out of the SSRF policy's reach, so
    check traffic connects directly (plain httpx would use the proxy)."""
    server, port, seen = await _proxy()
    env_proxy(port)
    async with _closing(server):
        result = await _b(HTTPCheck(_cfg(target="http://behind-proxy.invalid/")).run())
    assert not seen, f"check traffic went through HTTP_PROXY: {seen}"
    assert result["success"] is False


async def test_control_plain_httpx_honours_http_proxy_from_env(env_proxy) -> None:
    server, port, seen = await _proxy()
    env_proxy(port)
    async with _closing(server), httpx.AsyncClient() as client:
        response = await _b(client.get("http://behind-proxy.invalid/"))
    assert response.status_code == 200
    assert seen and b"behind-proxy.invalid" in seen[0]


# ── 5. ping ──────────────────────────────────────────────────────────────────


async def test_ping_never_hands_the_name_to_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []

    async def fake_run(*args: str, **kwargs: Any) -> tuple[int, bytes, bytes]:
        seen.append(args)
        return 2, b"", b"ping: unknown host"

    monkeypatch.setattr("app.checks.ping.run_subprocess_safely", fake_run)
    result = await _b(
        PingCheck(_cfg(check_type="ping", target="flaky-resolve.invalid")).run()
    )
    assert not any("flaky-resolve.invalid" in args for args in seen), seen
    assert result["success"] is False
    assert "Could not resolve host" in (result.get("error") or ""), result


async def test_ping_on_macos_does_not_pass_ipv6_to_ping(
    answers, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []

    async def fake_run(*args: str, **kwargs: Any) -> tuple[int, bytes, bytes]:
        seen.append(args)
        return 68, b"", b"ping: cannot resolve: Unknown host"

    monkeypatch.setattr("app.checks.ping.run_subprocess_safely", fake_run)
    monkeypatch.setattr("app.checks.ping.platform.system", lambda: "Darwin")
    answers["v6first.test"] = ["2001:db8::5", "192.0.2.5"]
    await _b(PingCheck(_cfg(check_type="ping", target="v6first.test")).run())
    assert seen
    assert not (seen[0][0] == "ping" and ":" in seen[0][-1]), seen
