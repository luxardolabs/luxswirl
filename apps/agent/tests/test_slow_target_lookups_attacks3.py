"""The certificate read frees its thread when its run's time is up, and a refusal
made under a policy the server changed mid-lookup is judged again.

No network: names resolve through a fake getaddrinfo, and every "connection" the
certificate probe makes is one end of a local socketpair whose other end this
test drives (a TLS server, a trickling peer, or nothing).
"""

from __future__ import annotations

import asyncio
import datetime
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpcore
import pytest
from app.checks import _lookups, _ssrf_policy
from app.checks.http import HTTPCheck
from app.shared import ssrf_http
from app.shared.ssrf import SsrfBlockedError
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

pytestmark = pytest.mark.unit

PUBLIC = "8.8.4.4"
PRIVATE = "10.0.0.7"


# --- fakes --------------------------------------------------------------------


class Resolver:
    def __init__(self) -> None:
        self.gate = threading.Event()

    def __call__(self, host: Any, port: Any, *a: Any, **k: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else str(host)
        p = int(port or 0)
        if name.endswith(".gated3.test"):
            self.gate.wait(20)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PRIVATE, p))]
        if name in {"healthy3.test", "slowtls3.test", "bücher.example"}:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, p))]
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> Iterator[Resolver]:
    r = Resolver()
    monkeypatch.setattr(socket, "getaddrinfo", r)
    monkeypatch.setitem(_ssrf_policy._policy, "block_private_networks", False)
    yield r
    r.gate.set()


class Peers:
    """socket.create_connection replaced: each call returns one end of a
    socketpair; `mode` says what the other end does."""

    def __init__(self, mode: str, server_ctx: ssl.SSLContext | None = None) -> None:
        self.mode = mode
        self.server_ctx = server_ctx
        self.dialled: list[tuple[str, int]] = []
        self.far: list[socket.socket] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        if mode == "trickle":
            t = threading.Thread(target=self._trickle, daemon=True)
            t.start()
            self.threads.append(t)

    def create_connection(self, address: Any, timeout: Any = None, *a: Any) -> Any:
        near, far = socket.socketpair()
        near.settimeout(timeout)
        with self.lock:
            self.dialled.append((address[0], int(address[1])))
            self.far.append(far)
        if self.mode == "close":
            far.close()
        elif self.mode == "tls":
            t = threading.Thread(target=self._serve, args=(far,), daemon=True)
            t.start()
            self.threads.append(t)
        elif self.mode == "trickle":
            # A TLS record header promising 16 KiB; the body comes a byte at a time.
            far.sendall(b"\x16\x03\x03\x40\x00")
        return near

    def _serve(self, far: socket.socket) -> None:
        assert self.server_ctx is not None
        try:
            far.settimeout(5)
            with self.server_ctx.wrap_socket(far, server_side=True) as s:
                s.recv(1)
        except OSError, ssl.SSLError:
            pass

    def _trickle(self) -> None:
        # One byte to each peer every 0.2 s: inside any per-operation socket
        # timeout over 0.2 s, so no single read ever times out.
        while not self.stop.wait(0.2):
            with self.lock:
                peers = list(self.far)
            for far in peers:
                try:
                    far.send(b"\x00")
                except OSError:
                    pass

    def close(self) -> None:
        self.stop.set()
        with self.lock:
            for far in self.far:
                try:
                    far.close()
                except OSError:
                    pass


def _server_ctx(tmp_path: Any) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "adv3.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(
            x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Adv3 CA")])
        )
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "c.pem"
    key_path = tmp_path / "k.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


def _peers(monkeypatch: pytest.MonkeyPatch, mode: str, ctx: Any = None) -> Peers:
    peers = Peers(mode, ctx)
    monkeypatch.setattr(socket, "create_connection", peers.create_connection)
    return peers


async def _drain(seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while _lookups._in_flight and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


def _cert_reads_in_flight() -> int:
    with _lookups._in_flight_lock:
        return sum(
            1
            for (fn, _args) in _lookups._in_flight
            if fn is HTTPCheck._read_certificate
        )


# --- GREEN controls: a healthy HTTPS target still gets its certificate ---------


@pytest.mark.parametrize(
    "target",
    [
        "https://healthy3.test/",
        "https://healthy3.test:8443/path?q=1",
        "https://[2001:4860:4860::8844]:8443/",
        "https://bücher.example/",
    ],
)
async def test_a_healthy_https_target_gets_certificate_details(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, target: str
) -> None:
    peers = _peers(monkeypatch, "tls", _server_ctx(tmp_path))
    try:
        info = await HTTPCheck._certificate(target, 2, 2.5)
    finally:
        peers.close()
    await _drain()
    assert info is not None, (target, peers.dialled)
    assert info["issuer"] == "Adv3 CA" and info["subject"] == "adv3.test", info
    expected_port = 8443 if ":8443" in target else 443
    assert all(port == expected_port for _, port in peers.dialled), peers.dialled


async def test_a_peer_that_hangs_up_frees_its_thread_at_once(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers = _peers(monkeypatch, "close")
    info = await HTTPCheck._certificate("https://slowtls3.test:9001/", 0.5, 0.6)
    await asyncio.sleep(0.5)
    left = _cert_reads_in_flight()
    peers.close()
    await _drain()
    assert info is None and left == 0, (info, left)


# --- A trickling TLS peer never holds a pool thread past the run --------------


async def test_a_trickling_tls_handshake_frees_its_thread(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    peers = _peers(monkeypatch, "trickle")
    begun = time.monotonic()
    info = await HTTPCheck._certificate("https://slowtls3.test:9002/", 0.5, 0.6)
    returned_after = time.monotonic() - begun
    # The run is long over (0.6 s budget, 0.5 s per-operation timeout). Give the
    # thread ten times its timeout to let go.
    await asyncio.sleep(5)
    held = _cert_reads_in_flight()
    peers.close()
    await _drain()
    assert info is None and returned_after < 1.0, (info, returned_after)
    assert held == 0, (
        f"the certificate read still holds a lookup thread {5 + returned_after:.1f} s "
        f"after a run whose budget was 0.6 s (timeout 0.5 s)"
    )


async def _healthy_lookup_after_cert_reads(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> Any:
    peers = _peers(monkeypatch, mode)
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 1.0)
    try:
        # LOOKUP_WORKERS HTTPS checks on one host an editor controls, each on its
        # own port (or each with its own timeout: either makes a distinct call).
        await asyncio.gather(
            *(
                HTTPCheck._certificate(f"https://slowtls3.test:{10000 + i}/", 0.5, 0.6)
                for i in range(_lookups.LOOKUP_WORKERS)
            )
        )
        await asyncio.sleep(0.3)
        # Every one of those runs is over. An unrelated ping's lookup, whose
        # resolver answers at once:
        try:
            return await _ssrf_policy.lookup_allowed_addresses("healthy3.test")
        except _lookups.LookupTimeoutError as e:
            return e
    finally:
        peers.close()
        await _drain(30)


async def test_cert_reads_that_end_leave_a_healthy_lookup_working(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = await _healthy_lookup_after_cert_reads(monkeypatch, "close")
    assert got == [PUBLIC], got


async def test_trickling_tls_peers_leave_every_checks_lookups_working(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = await _healthy_lookup_after_cert_reads(monkeypatch, "trickle")
    assert got == [PUBLIC], f"an unrelated, instant lookup got {got!r}"


# --- A refusal under a policy that changed mid-lookup -------------------------


async def test_a_private_host_under_the_strict_policy_is_refused(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(_ssrf_policy._policy, "block_private_networks", True)
    resolver.gate.set()
    with pytest.raises(SsrfBlockedError):
        await _ssrf_policy.lookup_allowed_addresses("a.gated3.test")


async def test_a_lookup_refused_under_a_policy_turned_off_mid_lookup_is_judged_again(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(_ssrf_policy._policy, "block_private_networks", True)
    task = asyncio.create_task(_ssrf_policy.lookup_allowed_addresses("b.gated3.test"))
    await asyncio.sleep(0.2)
    # The server turns private-network blocking off while the name resolves.
    _ssrf_policy.apply_server_policy({"block_private_networks": False})
    resolver.gate.set()
    got: Any
    try:
        got = await task
    except SsrfBlockedError as e:
        got = e
    assert got == [PRIVATE], (
        f"under the policy in force on return the address is allowed; got {got!r}"
    )


async def test_a_target_refused_under_a_policy_turned_off_mid_lookup_is_judged_again(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(_ssrf_policy._policy, "block_private_networks", True)
    task = asyncio.create_task(
        _ssrf_policy.target_allowed(
            "postgresql://u:p@c.gated3.test:5432/db", database=True
        )
    )
    await asyncio.sleep(0.2)
    _ssrf_policy.apply_server_policy({"block_private_networks": False})
    resolver.gate.set()
    got: Any = None
    try:
        await task
    except SsrfBlockedError as e:
        got = e
    assert got is None, (
        f"judged under the policy in force on return it is allowed; got {got!r}"
    )


# --- The backend's transport: no lookup given, `allowed` runs as before -------


async def test_the_backend_transport_without_a_lookup_dials_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialled: list[tuple[str, int]] = []

    async def connect_tcp(self: Any, host: str, port: int, *a: Any, **k: Any) -> Any:
        dialled.append((host, port))
        raise httpcore.ConnectError("recorded, not made")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)
    backend = ssrf_http.VettedBackend(lambda host: [PUBLIC] if host == "x.test" else [])
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("x.test", 443, timeout=1)
    assert dialled == [(PUBLIC, 443)], dialled


# --- Many dead addresses never hold the thread past the run's time ------------

MANY = [f"8.8.{i}.{i}" for i in range(1, 21)]


class Blackholes:
    """getaddrinfo answers MANY public addresses for many3.test; every connect
    to them waits its whole timeout and fails, as a dropped SYN does."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.dials = 0
        self.lock = threading.Lock()

    def getaddrinfo(self, host: Any, port: Any, *a: Any, **k: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else str(host)
        p = int(port or 0)
        if name == "many3.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, p)) for ip in MANY]
        if name == "healthy3.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, p))]
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    def create_connection(self, address: Any, timeout: Any = None, *a: Any) -> Any:
        with self.lock:
            self.dials += 1
        self.stop.wait(timeout)
        raise TimeoutError("timed out")


@pytest.fixture
def blackholes(monkeypatch: pytest.MonkeyPatch) -> Iterator[Blackholes]:
    b = Blackholes()
    monkeypatch.setattr(socket, "getaddrinfo", b.getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", b.create_connection)
    monkeypatch.setitem(_ssrf_policy._policy, "block_private_networks", False)
    yield b
    b.stop.set()


async def test_a_certificate_read_over_many_dead_addresses_frees_its_thread_in_time(
    blackholes: Blackholes,
) -> None:
    begun = time.monotonic()
    info = await HTTPCheck._certificate("https://many3.test/", 0.5, 0.6)
    returned_after = time.monotonic() - begun
    await asyncio.sleep(3)
    held = _cert_reads_in_flight()
    dials = blackholes.dials
    blackholes.stop.set()
    await _drain()
    assert info is None and returned_after < 1.0, (info, returned_after)
    assert held == 0, (
        f"{3 + returned_after:.1f} s after a run whose budget was 0.6 s, the read "
        f"still holds its thread, {dials} dials in, one per address, each 0.5 s"
    )


async def test_certificate_reads_over_dead_addresses_leave_every_checks_lookups_working(
    blackholes: Blackholes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 1.0)
    await asyncio.gather(
        *(
            HTTPCheck._certificate(f"https://many3.test:{20000 + i}/", 0.5, 0.6)
            for i in range(_lookups.LOOKUP_WORKERS)
        )
    )
    await asyncio.sleep(0.3)
    try:
        got: Any = await _ssrf_policy.lookup_allowed_addresses("healthy3.test")
    except _lookups.LookupTimeoutError as e:
        got = e
    blackholes.stop.set()
    await _drain(30)
    assert got == [PUBLIC], f"an unrelated, instant lookup got {got!r}"
