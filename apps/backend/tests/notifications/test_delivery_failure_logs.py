"""A failed notification leaves no part of its endpoint's URL in the server log.

An HTTP library's error names the full request URL, and a notification
endpoint's path can be its credential (a Home Assistant webhook id, a chat
hook's token). A failed delivery, and a failed test send, are logged once,
as a warning with the stored error (status, or the error with each URL cut
to its host), never with a traceback that quotes the request.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import types

import aiosmtplib
import httpx
import pytest
from app.notifications.providers import email as email_mod
from app.notifications.providers import homeassistant as ha_mod
from app.notifications.providers import webhook as hook_mod
from app.notifications.providers.base import NotificationContext
from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import make_alert, make_notification_provider
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

HA_URL = "https://10.0.0.5/api/webhook/ha-id-s3cret"
HOOK_URL = "https://10.0.0.6/hooks/T000/B000/slack-tok-s3cret"
SECRETS = (
    "ha-id-s3cret",
    "slack-tok-s3cret",
    "/api/webhook",
    "/hooks/T000",
    "hdr-tok-s3cret",
)


def _httpx(handler) -> types.SimpleNamespace:
    """An httpx stand-in whose endpoint answers every POST through `handler`."""
    transport = httpx.MockTransport(handler)

    def client(**kwargs):
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=transport, **kwargs)

    return types.SimpleNamespace(
        AsyncClient=client,
        HTTPStatusError=httpx.HTTPStatusError,
        RequestError=httpx.RequestError,
        TimeoutException=httpx.TimeoutException,
    )


def _refusing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"cannot reach {request.url}", request=request)


def _illegal_header(request: httpx.Request) -> httpx.Response:
    # h11's text for a configured header value with a newline in it. A mock:
    # a real client leaves that connection's socket unclosed.
    raise httpx.LocalProtocolError("Illegal header value b'hdr-tok-s3cret\\n'")


def _context() -> NotificationContext:
    return NotificationContext(
        check_name="web",
        check_type="http",
        target="https://site.example.test",
        agent_id=None,
        status="error",
        alert_name="alert",
    )


def _assert_url_free(caplog: pytest.LogCaptureFixture, message: str) -> None:
    failures = [r for r in caplog.records if r.getMessage() == message]
    assert len(failures) == 2, caplog.text
    assert all(r.levelno == logging.WARNING and not r.exc_info for r in failures)
    for secret in SECRETS:
        assert secret not in caplog.text, secret


@pytest.mark.parametrize("handler", [_refusing, _unreachable, _illegal_header])
async def test_a_failed_delivery_logs_no_endpoint_path(
    db: AsyncSession, monkeypatch, caplog, handler
) -> None:
    monkeypatch.setattr(ha_mod, "httpx", _httpx(handler))
    monkeypatch.setattr(hook_mod, "httpx", _httpx(handler))
    ha = make_notification_provider(
        provider_type="homeassistant", config={"post_url": HA_URL}
    )
    hook = make_notification_provider(
        provider_type="webhook", config={"post_url": HOOK_URL}
    )
    alert = make_alert()
    db.add_all([ha, hook, alert])
    await db.flush()

    with caplog.at_level(logging.DEBUG):
        rows = await NotificationCoreService.send_notifications(
            db,
            [ha.id, hook.id],
            _context(),
            alert_id=alert.id,
            check_result_id=None,
            check_result_timestamp=None,
        )

    assert [row.status for row in rows] == ["failed", "failed"]
    _assert_url_free(caplog, "Notification send failed")


@pytest.mark.parametrize("handler", [_refusing, _unreachable, _illegal_header])
async def test_a_failed_test_send_logs_no_endpoint_path(
    db: AsyncSession, monkeypatch, caplog, handler
) -> None:
    monkeypatch.setattr(ha_mod, "httpx", _httpx(handler))
    monkeypatch.setattr(hook_mod, "httpx", _httpx(handler))
    ha = make_notification_provider(
        provider_type="homeassistant", config={"post_url": HA_URL}
    )
    hook = make_notification_provider(
        provider_type="webhook", config={"post_url": HOOK_URL}
    )
    db.add_all([ha, hook])
    await db.flush()

    with caplog.at_level(logging.DEBUG):
        results = [
            await NotificationCoreService.test_provider(db, provider.id)
            for provider in (ha, hook)
        ]

    assert [ok for ok, _ in results] == [False, False]
    _assert_url_free(caplog, "Test notification error")


async def test_a_failed_email_is_logged_once_without_a_traceback(
    db: AsyncSession, monkeypatch, caplog
) -> None:
    async def refuse(*args, **kwargs):
        raise aiosmtplib.SMTPAuthenticationError(535, "authentication failed")

    monkeypatch.setattr(email_mod, "aiosmtplib", types.SimpleNamespace(send=refuse))
    mail = make_notification_provider(
        provider_type="email",
        config={
            "hostname": "smtp.example.test",
            "port": 587,
            "from_email": "luxswirl@example.test",
            "to_email": "ops@example.test",
            "username": "luxswirl",
            "password": "smtp-pw-s3cret",
        },
    )
    alert = make_alert()
    db.add_all([mail, alert])
    await db.flush()

    with caplog.at_level(logging.DEBUG):
        rows = await NotificationCoreService.send_notifications(
            db,
            [mail.id],
            _context(),
            alert_id=alert.id,
            check_result_id=None,
            check_result_timestamp=None,
        )

    assert [row.status for row in rows] == ["failed"]
    assert not any(r.exc_info for r in caplog.records), caplog.text
    failures = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in failures] == ["Notification send failed"]
    assert "smtp-pw-s3cret" not in caplog.text


HEADER_SECRET = "hdr-tok-s3cret"
SMTP_PASSWORD = "smtp-pw-s3cret"
SENT_SECRETS = (
    "path-tok-s3cret",
    "q-s3cret",
    HEADER_SECRET,
    SMTP_PASSWORD,
    base64.b64encode(b"\0luxswirl\0" + SMTP_PASSWORD.encode()).decode(),
    base64.b64encode(SMTP_PASSWORD.encode()).decode(),
)


def _answer_500(request: bytes) -> bytes:
    return b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n"


def _echo(request: bytes) -> bytes:
    return request


def _reason_echo(request: bytes) -> bytes:
    """A 404 whose reason phrase quotes the request line and our header."""
    line = request.split(b"\r\n", 1)[0]
    token = re.search(rb"(?i)x-hook-token: ([^\r]*)", request)
    quoted = token.group(1) if token else b"-"
    return (
        b"HTTP/1.1 404 No route "
        + line
        + b" token "
        + quoted
        + b"\r\nContent-Length: 0\r\n\r\n"
    )


async def _endpoint(reply) -> tuple[asyncio.Server, int]:
    """A real endpoint answering each request with `reply(request)`: a mock
    transport never runs h11, whose errors quote what it refused."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            writer.write(reply(await asyncio.wait_for(reader.read(65536), 2)))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _smtp(mode: str) -> tuple[asyncio.Server, int]:
    """A real SMTP server that refuses the login, or echoes our commands
    back (an echoed line is not a reply, so the client quotes it)."""
    methods = b"LOGIN" if mode == "echo-login" else b"PLAIN"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            writer.write(b"220 mail.test ESMTP\r\n")
            await writer.drain()
            while line := await asyncio.wait_for(reader.readline(), 3):
                if line.upper().startswith((b"EHLO", b"HELO")):
                    writer.write(b"250-mail.test\r\n250 AUTH " + methods + b"\r\n")
                elif mode == "refuse":
                    writer.write(b"535 5.7.8 authentication failed\r\n")
                elif mode == "echo-login" and line.upper().startswith(b"AUTH"):
                    writer.write(b"334 UGFzc3dvcmQ6\r\n")
                else:
                    writer.write(line)
                await writer.drain()
        except OSError, TimeoutError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join([caplog.text, *(repr(r.__dict__) for r in caplog.records)])


async def _fail_both_ways(
    db: AsyncSession, caplog, provider_type: str, config: dict
) -> list[str]:
    """Deliver once and test-send once; every text either can be read in."""
    provider = make_notification_provider(provider_type=provider_type, config=config)
    alert = make_alert()
    db.add_all([provider, alert])
    await db.flush()
    with caplog.at_level(logging.DEBUG):
        rows = await NotificationCoreService.send_notifications(
            db,
            [provider.id],
            _context(),
            alert_id=alert.id,
            check_result_id=None,
            check_result_timestamp=None,
        )
        ok, error = await NotificationCoreService.test_provider(db, provider.id)
    assert [row.status for row in rows] == ["failed"]
    assert not ok
    assert rows[0].error_message == error
    return [_logged(caplog), error or ""]


def _assert_nothing_sent_is_quoted(texts: list[str]) -> None:
    for text in texts:
        for secret in SENT_SECRETS:
            assert secret not in text, secret


@pytest.mark.parametrize(
    ("reply", "header", "stored"),
    [
        (_answer_500, HEADER_SECRET, "HTTP 500 Internal Server Error"),
        (_echo, HEADER_SECRET, "RemoteProtocolError"),
        (_reason_echo, HEADER_SECRET, "HTTP 404 Not Found"),
    ],
)
@pytest.mark.parametrize("provider_type", ["webhook", "homeassistant"])
async def test_what_an_http_endpoint_sees_or_says_is_never_quoted(
    db: AsyncSession, caplog, provider_type: str, reply, header: str, stored: str
) -> None:
    server, port = await _endpoint(reply)
    config: dict = {
        "post_url": f"http://127.0.0.1:{port}/hooks/path-tok-s3cret?sig=q-s3cret"
    }
    if provider_type == "webhook":
        config["additional_headers"] = {"X-Hook-Token": header}
    try:
        texts = await _fail_both_ways(db, caplog, provider_type, config)
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()

    assert texts[1].startswith(stored), texts[1]
    _assert_nothing_sent_is_quoted(texts)


@pytest.mark.parametrize(
    ("mode", "stored"),
    [
        ("refuse", "SMTPAuthenticationError (SMTP 535)"),
        ("echo-plain", "SMTPResponseException (ValueError)"),
        ("echo-login", "SMTPResponseException (ValueError)"),
    ],
)
async def test_what_an_smtp_server_sees_or_says_is_never_quoted(
    db: AsyncSession, caplog, mode: str, stored: str
) -> None:
    server, port = await _smtp(mode)
    config = {
        "hostname": "127.0.0.1",
        "port": port,
        "security": "none",
        "from_email": "luxswirl@example.test",
        "to_email": "ops@example.test",
        "username": "luxswirl",
        "password": SMTP_PASSWORD,
        "timeout_seconds": 5,
    }
    try:
        texts = await _fail_both_ways(db, caplog, "email", config)
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()

    assert texts[1] == stored, texts[1]
    _assert_nothing_sent_is_quoted(texts)


async def test_an_unreachable_endpoint_names_the_os_error(
    db: AsyncSession, caplog
) -> None:
    server, port = await _endpoint(_answer_500)
    server.close()
    await server.wait_closed()

    texts = await _fail_both_ways(
        db,
        caplog,
        "webhook",
        {"post_url": f"http://127.0.0.1:{port}/hooks/path-tok-s3cret"},
    )

    assert texts[1] == "ConnectError (ConnectionRefusedError): Connection refused"
    _assert_nothing_sent_is_quoted(texts)
