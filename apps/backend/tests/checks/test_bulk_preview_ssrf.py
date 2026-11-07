"""The bulk preview fetches from the server, so it is held to the SSRF settings: a
blocked address gets no connection, a name is pinned to the address it was vetted on,
a redirect is reported and not followed, and one request's cost is bounded.

Every connection here goes to a local server or is refused before it opens.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime
import os
import socket
import ssl
import struct
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpcore
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.services.core import check_preview_core_service
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_preview_core_service import CheckPreviewCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.ssrf_http import VettedBackend, VettedTransport
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

METADATA = "169.254.169.254"
OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


@dataclass
class Local:
    """A local server and what it was sent."""

    server: asyncio.Server
    port: int
    requests: list[bytes] = field(default_factory=list)


async def _serve(
    answer: Callable[[asyncio.StreamWriter], Awaitable[None]],
) -> Local:
    requests: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        # A client that hangs up mid-answer is what a slow answer is waiting for.
        with contextlib.suppress(ConnectionError):
            await answer(writer)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return Local(server, server.sockets[0].getsockname()[1], requests)


async def _ok(writer: asyncio.StreamWriter) -> None:
    writer.write(OK)
    await writer.drain()


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[list[str]]]:
    """Each name's resolver answers, in turn; the last repeats. A numeric host goes to
    the real resolver, which reads it without the network."""
    table: dict[str, list[list[str]]] = {}
    real = socket.getaddrinfo

    def fake(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in table:
            return real(host, *args, **kwargs)
        turns = table[host]
        addresses = turns.pop(0) if len(turns) > 1 else turns[0]
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every address a socket was opened on."""
    seen: list[str] = []
    real = httpcore.AnyIOBackend.connect_tcp

    async def spy(self: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(host)
        return await real(self, host, *args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", spy)
    return seen


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as http:
        yield http


async def _block_private_networks(db: AsyncSession) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await SettingsCoreService.update_setting(
        db, "security.block_private_networks", True
    )


async def _row(db: AsyncSession, url: str) -> dict[str, Any]:
    [row] = await CheckPreviewCoreService.bulk_preview_checks(db, [url])
    return row


@pytest.mark.parametrize(
    "url",
    [
        f"http://{METADATA}/latest/meta-data/",
        METADATA,
        "http://2852039166/",
        "http://0xa9fea9fe/",
        "http://[::ffff:169.254.169.254]/",
        "http://100.100.100.200/",
    ],
)
async def test_a_metadata_address_gets_no_connection(
    db: AsyncSession, opened: list[str], url: str
) -> None:
    row = await _row(db, url)

    assert opened == []
    assert row["validation_status"] == "error"
    assert "blocked" in row["validation_message"]
    assert row["status_code"] is None


async def test_a_private_address_follows_the_setting(
    db: AsyncSession, opened: list[str]
) -> None:
    local = await _serve(_ok)
    url = f"http://127.0.0.1:{local.port}/"
    async with local.server:
        allowed = await _row(db, url)
        await _block_private_networks(db)
        refused = await _row(db, url)

    assert (allowed["validation_status"], allowed["status_code"]) == ("success", 200)
    assert refused["validation_status"] == "error"
    assert "blocked" in refused["validation_message"]
    assert len(local.requests) == 1
    assert opened == ["127.0.0.1"]


async def test_a_name_that_rebinds_after_the_check_gets_no_connection(
    db: AsyncSession, answers: dict[str, list[list[str]]], opened: list[str]
) -> None:
    answers["rebind.test"] = [["127.0.0.1"], [METADATA]]

    row = await _row(db, "http://rebind.test/")

    assert opened == []
    assert row["validation_status"] == "error"
    assert "blocked" in row["validation_message"]


async def test_a_name_is_dialed_on_its_vetted_address_and_keeps_its_host_header(
    db: AsyncSession, answers: dict[str, list[list[str]]], opened: list[str]
) -> None:
    local = await _serve(_ok)
    answers["pi.hole"] = [["127.0.0.1"]]
    async with local.server:
        row = await _row(db, f"http://pi.hole:{local.port}/")

    assert row["status_code"] == 200
    assert opened == ["127.0.0.1"]
    assert f"host: pi.hole:{local.port}".encode() in local.requests[0].lower()


async def test_a_name_with_one_blocked_answer_is_refused_as_saving_it_is(
    db: AsyncSession, answers: dict[str, list[list[str]]], opened: list[str]
) -> None:
    answers["both.test"] = [["127.0.0.1", METADATA]]

    row = await _row(db, "http://both.test/")

    assert opened == []
    assert row["validation_status"] == "error"
    assert "Settings > Security" in row["validation_message"]


async def test_a_redirect_is_reported_and_not_followed(
    db: AsyncSession, opened: list[str]
) -> None:
    async def redirect(writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 302 Found\r\n"
            b"Location: http://169.254.169.254/latest/meta-data/\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()

    local = await _serve(redirect)
    async with local.server:
        row = await _row(db, f"http://127.0.0.1:{local.port}/")

    assert row["status_code"] == 302
    assert opened == ["127.0.0.1"]


async def test_the_status_is_read_without_waiting_for_the_body(
    db: AsyncSession,
) -> None:
    async def endless(writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000000000\r\n\r\n")
        for _ in range(400):
            writer.write(b"x" * 65536)
            await writer.drain()
            await asyncio.sleep(0.01)

    local = await _serve(endless)
    started = time.monotonic()
    async with local.server:
        row = await asyncio.wait_for(
            _row(db, f"http://127.0.0.1:{local.port}/"), timeout=4
        )
        elapsed = time.monotonic() - started

    assert (row["validation_status"], row["status_code"]) == ("success", 200)
    assert elapsed < 3


async def test_a_response_head_that_never_ends_meets_the_deadline(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_preview_core_service, "PROBE_SECONDS", 1)

    async def trickle(writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\n")
        for _ in range(100):
            writer.write(b"X-A: b\r\n")
            await writer.drain()
            await asyncio.sleep(0.1)

    local = await _serve(trickle)
    async with local.server:
        row = await asyncio.wait_for(
            _row(db, f"http://127.0.0.1:{local.port}/"), timeout=4
        )

    assert row["validation_status"] == "error"
    assert row["validation_message"] == "Timeout (1s)"


async def test_a_non_http_target_is_judged_and_not_connected(
    db: AsyncSession, opened: list[str]
) -> None:
    blocked = await _row(db, f"tcp://{METADATA}:80")
    allowed = await _row(db, "tcp://192.0.2.1:80")

    assert blocked["validation_status"] == "error"
    assert "blocked" in blocked["validation_message"]
    assert allowed["validation_status"] == "unknown"
    assert opened == []


async def test_urls_past_the_limit_are_named_and_not_looked_up(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_preview_core_service, "PROBED_URLS", 2)
    looked_up: list[str] = []
    real = socket.getaddrinfo

    def spy(host: Any, *args: Any, **kwargs: Any) -> Any:
        looked_up.append(host)
        return real("192.0.2.1", *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", spy)

    rows = await CheckPreviewCoreService.bulk_preview_checks(
        db,
        ["tcp://a.test:1", "tcp://b.test:1", f"http://{METADATA}/", "tcp://c.test:1"],
    )

    assert sorted(looked_up) == ["a.test", "b.test"]
    assert [row["validation_status"] for row in rows] == ["unknown"] * 4
    assert rows[1]["validation_message"] == ""
    assert rows[2]["validation_message"].startswith("Not checked: ")
    assert rows[2]["generated_name"]


async def test_probes_run_a_bounded_number_at_once(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_preview_core_service, "PROBES_AT_ONCE", 3)
    running = 0
    most = 0

    async def held(writer: asyncio.StreamWriter) -> None:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.05)
        running -= 1
        writer.write(OK)
        await writer.drain()

    local = await _serve(held)
    async with local.server:
        rows = await CheckPreviewCoreService.bulk_preview_checks(
            db, [f"http://127.0.0.1:{local.port}/{i}" for i in range(12)]
        )

    assert [row["status_code"] for row in rows] == [200] * 12
    assert most == 3


async def _signed_in(client: AsyncClient, db: AsyncSession, role: str) -> None:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)


async def test_the_route_shows_an_editor_the_refusal(
    client: AsyncClient, db: AsyncSession, opened: list[str]
) -> None:
    await _signed_in(client, db, "editor")

    resp = await client.post(
        "/checks/bulk-preview", data={"bulk_urls": f"http://{METADATA}/x"}
    )

    assert resp.status_code == 200
    assert "blocked cloud metadata" in resp.text
    assert opened == []


async def test_the_route_refuses_a_viewer(
    client: AsyncClient, db: AsyncSession, opened: list[str]
) -> None:
    await _signed_in(client, db, "viewer")

    resp = await client.post(
        "/checks/bulk-preview", data={"bulk_urls": "http://192.0.2.1/x"}
    )

    assert resp.status_code in (302, 303, 307, 401, 403), resp.status_code
    assert opened == []


@pytest.mark.parametrize(
    "first_line",
    [
        b"SSH-2.0-OpenSSH_9.6 TOPSECRET\r\n",
        b"-NOAUTH TOPSECRET required.\r\n",
        b"HTTP/1.1 200 OK\r\nSet-Cookie TOPSECRET=1\r\n\r\n",
    ],
)
async def test_what_a_service_sends_never_reaches_the_row(
    db: AsyncSession, first_line: bytes
) -> None:
    async def service(writer: asyncio.StreamWriter) -> None:
        writer.write(first_line)
        await writer.drain()

    local = await _serve(service)
    async with local.server:
        row = await _row(db, f"http://127.0.0.1:{local.port}/")

    assert row["validation_status"] == "error"
    assert row["validation_message"] == "The target did not answer HTTP"


def _self_signed() -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*_pem_files(cert, key))
    return context


def _pem_files(cert: x509.Certificate, key: ec.EllipticCurvePrivateKey) -> list[str]:
    paths = []
    for data in (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    ):
        descriptor, path = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
        paths.append(path)
    return paths


async def test_a_self_signed_target_gets_the_certificate_warning(
    db: AsyncSession,
) -> None:
    async def handle(_r: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=_self_signed())
    port = server.sockets[0].getsockname()[1]
    async with server:
        row = await _row(db, f"https://127.0.0.1:{port}/")

    assert row["validation_status"] == "warning"
    assert "verify_ssl=False" in row["validation_message"]


async def test_https_to_a_port_speaking_plain_http_is_a_failed_handshake(
    db: AsyncSession,
) -> None:
    async def plain(_r: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(OK)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(plain, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        row = await _row(db, f"https://127.0.0.1:{port}/")

    assert (row["validation_status"], row["validation_message"]) == (
        "error",
        "TLS handshake failed",
    )


async def test_a_name_with_no_address_says_so(
    db: AsyncSession, answers: dict[str, list[list[str]]]
) -> None:
    answers["gone.test"] = [[]]

    row = await _row(db, "http://gone.test/")

    assert row["validation_message"] == "Could not resolve the host"


async def test_a_port_that_refuses_is_a_failed_connection(db: AsyncSession) -> None:
    server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    row = await _row(db, f"http://127.0.0.1:{port}/")

    assert row["validation_message"] == "Connection failed"


async def test_a_dead_first_address_leaves_time_for_the_next(
    db: AsyncSession,
    answers: dict[str, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = await _serve(_ok)
    real = httpcore.AnyIOBackend.connect_tcp

    async def black_hole(self: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        if host == "203.0.113.9":
            await asyncio.sleep(kwargs.get("timeout") or 5)
            raise httpcore.ConnectTimeout("no answer")
        return await real(self, host, *args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", black_hole)
    answers["dual.test"] = [["203.0.113.9", "127.0.0.1"]]
    async with local.server:
        row = await _row(db, f"http://dual.test:{local.port}/")

    assert row["status_code"] == 200, row


async def test_a_slow_nameserver_leaves_the_default_executor_free(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_preview_core_service, "PROBE_SECONDS", 1)
    release = threading.Event()
    real = socket.getaddrinfo

    def slow(host: Any, *args: Any, **kwargs: Any) -> Any:
        if str(host).endswith(".slow.test"):
            release.wait(10)
            raise socket.gaierror("no answer")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    try:
        rows = await asyncio.wait_for(
            CheckPreviewCoreService.bulk_preview_checks(
                db, [f"http://h{i}.slow.test/" for i in range(100)]
            ),
            timeout=8,
        )
        started = time.monotonic()
        await asyncio.to_thread(lambda: None)
        waited = time.monotonic() - started
    finally:
        release.set()

    assert {row["validation_message"] for row in rows} == {"Timeout (1s)"}
    assert waited < 0.5


async def test_a_row_that_does_not_parse_has_a_fixed_name(db: AsyncSession) -> None:
    rows = await CheckPreviewCoreService.bulk_preview_checks(
        db, ["http://user:S3cret/PW@[/", "http://user:S3cret/PW@[/"]
    )

    assert [row["generated_name"] for row in rows] == [
        "invalid-url",
        "invalid-url (2)",
    ]


@pytest.mark.parametrize("scheme", ["https", "http"])
async def test_a_cancel_mid_connection_closes_the_socket(scheme: str) -> None:
    hung_up = asyncio.Event()

    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Bounded, so a socket left open fails this test rather than hanging the suite.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(reader.read(), timeout=3)
            hung_up.set()
        writer.close()

    server = await asyncio.start_server(stall, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transport = VettedTransport(verify=True, allowed=lambda _host: ["127.0.0.1"])
    async with server, AsyncClient(transport=transport, timeout=30) as client:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await client.get(f"{scheme}://127.0.0.1:{port}/")
        await asyncio.wait_for(hung_up.wait(), timeout=2)


async def test_a_url_naming_many_hosts_is_not_looked_up(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked_up: list[str] = []
    real = socket.getaddrinfo

    def spy(host: Any, *args: Any, **kwargs: Any) -> Any:
        looked_up.append(host)
        return real("192.0.2.1", *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", spy)
    limit = check_preview_core_service.HOSTS_PER_URL

    within = ",".join(f"h{i}.test" for i in range(limit))
    over = ",".join(f"m{i}.test" for i in range(limit + 1))
    allowed = await _row(db, f"postgres://u@{within}/db")
    assert sorted(looked_up) == sorted(f"h{i}.test" for i in range(limit))
    looked_up.clear()

    row = await _row(db, f"postgres://u@{over}/db")

    assert allowed["validation_status"] == "unknown"
    assert allowed["validation_message"] == ""
    assert looked_up == []
    assert row["validation_status"] == "unknown"
    assert row["validation_message"].startswith("Not checked: ")


async def test_a_slow_tls_handshake_on_the_only_address_gets_its_answer(
    db: AsyncSession,
) -> None:
    context = _self_signed()

    async def slow(_r: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(2)
        with contextlib.suppress(ssl.SSLError, ConnectionError):
            await writer.start_tls(context)
        writer.close()

    server = await asyncio.start_server(slow, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        row = await _row(db, f"https://127.0.0.1:{port}/")

    assert row["validation_status"] == "warning", row


async def test_a_slow_connect_on_the_only_address_gets_its_answer(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = await _serve(_ok)
    real = httpcore.AnyIOBackend.connect_tcp

    async def slow(self: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(2)
        if kwargs["timeout"] < 2:
            raise httpcore.ConnectTimeout("no answer")
        return await real(self, host, *args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", slow)
    async with local.server:
        row = await _row(db, f"http://127.0.0.1:{local.port}/")

    assert row["status_code"] == 200, row


async def test_a_parse_error_never_quotes_the_url(db: AsyncSession) -> None:
    url = "password=a?b \uff20S3CRETMARK"

    row = await _row(db, url)

    assert "S3CRETMARK" not in row["validation_message"]
    assert row["validation_message"] == "Parse error: The URL could not be parsed"
    with pytest.raises(ValueError, match="could not be parsed"):
        CheckCoreService.bulk_display_name(urlparse(f"https://{url}"))


async def test_a_reset_connection_is_a_failed_connection(db: AsyncSession) -> None:
    async def reset(writer: asyncio.StreamWriter) -> None:
        writer.get_extra_info("socket").setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        writer.transport.abort()

    local = await _serve(reset)
    async with local.server:
        row = await _row(db, f"http://127.0.0.1:{local.port}/")

    assert row["validation_message"] == "Connection failed"


@pytest.mark.parametrize("url", ["http:foo", "https://:80/", "https://a..b/"])
async def test_a_url_no_client_can_fetch_says_so(db: AsyncSession, url: str) -> None:
    row = await _row(db, url)

    assert row["validation_status"] == "error"
    assert row["validation_message"] == "The URL could not be parsed"


async def test_a_lookup_runs_in_its_callers_context() -> None:
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="")
    marker.set("caller")
    seen: list[str] = []

    def allowed(_host: str) -> list[str]:
        seen.append(marker.get())
        return []

    with pytest.raises(httpcore.ConnectError):
        await VettedBackend(allowed).connect_tcp("h.test", 80)

    assert seen == ["caller"]


@pytest.mark.parametrize(
    "url",
    [
        f"postgres:?host={METADATA}",
        f"postgres:?host={METADATA}:x:y",
        f"postgres://u@/d?host=[::ffff:{METADATA}%2525]",
        "postgres:5432",
    ],
)
async def test_a_database_url_with_no_slashes_is_judged_as_its_driver_reads_it(
    db: AsyncSession, url: str, opened: list[str]
) -> None:
    row = await _row(db, url)

    assert opened == []
    assert row["validation_status"] == "error"
    assert "blocked" in row["validation_message"]
