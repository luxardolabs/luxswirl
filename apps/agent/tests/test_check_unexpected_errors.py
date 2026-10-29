"""A check's failure is its result, and the log never floods with it.

The failures a check expects (a refused connection, a timeout, a DNS error, a
blocked target, a setting the library refuses) are each caught by name and
reported in the result alone. Anything else is still a failed result
("Unexpected error: ..."), and its traceback is logged at DEBUG: a debug run
can see it, and no failure that recurs every interval writes a traceback to
the normal log.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from uuid import uuid4

import pytest
from app.checks import dns as dns_mod
from app.checks import http as http_mod
from app.checks import json as json_mod
from app.checks import mysql as mysql_mod
from app.checks import ping as ping_mod
from app.checks import postgres as postgres_mod
from app.checks import synthetic as synthetic_mod
from app.checks.dns import DNSCheck
from app.checks.http import HTTPCheck
from app.checks.json import JSONCheck
from app.checks.mysql import MySQLCheck
from app.checks.ping import PingCheck
from app.checks.postgres import PostgreSQLCheck
from app.checks.synthetic import SyntheticCheck
from app.checks.tcp import TCPCheck

pytestmark = pytest.mark.unit


class Boom(RuntimeError):
    """An error no check expects."""


async def _boom(*args, **kwargs):
    raise Boom("unexpected")


def _cfg(check_type: str, target: str, **overrides) -> dict:
    return {
        "check_id": str(uuid4()),
        "name": f"{check_type}-unexpected",
        "check_type": check_type,
        "target": target,
        "retries": 1,
        **overrides,
    }


def _http(monkeypatch):
    monkeypatch.setattr(http_mod, "ssrf_guarded_send", _boom)
    return HTTPCheck(_cfg("http", "http://127.0.0.1:1/health"))


def _json(monkeypatch):
    monkeypatch.setattr(json_mod, "ssrf_guarded_send", _boom)
    return JSONCheck(
        _cfg("json", "http://127.0.0.1:1/", json_path="status", expected_value="ok")
    )


def _tcp(monkeypatch):
    monkeypatch.setattr(TCPCheck, "_get_address_info", _boom)
    return TCPCheck(_cfg("tcp", "127.0.0.1", port=1))


def _ping(monkeypatch):
    monkeypatch.setattr(ping_mod, "run_subprocess_safely", _boom)
    return PingCheck(_cfg("ping", "127.0.0.1"))


def _dns(monkeypatch):
    monkeypatch.setattr(dns_mod.dns.asyncresolver.Resolver, "resolve", _boom)
    return DNSCheck(
        _cfg("dns", "example.test", nameserver="127.0.0.1", record_type="A")
    )


def _mysql(monkeypatch):
    pytest.importorskip("aiomysql")
    monkeypatch.setattr(mysql_mod.aiomysql, "connect", _boom)
    return MySQLCheck(_cfg("mysql", "mysql://u:p@127.0.0.1:3306/db"))


def _postgres(monkeypatch):
    pytest.importorskip("asyncpg")
    monkeypatch.setattr(postgres_mod.asyncpg, "connect", _boom)
    return PostgreSQLCheck(_cfg("postgres", "postgresql://u:p@127.0.0.1:5432/db"))


@pytest.mark.parametrize(
    "make_check", [_http, _json, _tcp, _ping, _dns, _mysql, _postgres]
)
async def test_an_unexpected_error_fails_the_check_and_is_logged(
    monkeypatch, caplog, make_check
) -> None:
    check = make_check(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = await check.run()

    assert result["success"] is False
    assert "unexpected" in (result["error"] or "")
    logged = [r for r in caplog.records if r.exc_info and r.exc_info[0] is Boom]
    assert len(logged) == 1, caplog.text
    assert logged[0].levelno == logging.DEBUG


async def test_a_blocked_tcp_target_is_reported_as_blocked_not_logged(
    caplog,
) -> None:
    check = TCPCheck(_cfg("tcp", "169.254.169.254", port=80))

    with caplog.at_level(logging.DEBUG):
        result = await check.run()

    assert result["success"] is False
    assert result["error"].startswith("Target blocked: 169.254.169.254"), result
    assert not [r for r in caplog.records if r.exc_info], caplog.text


class _NoBrowser:
    """`async_playwright()` whose start fails, as with no browser installed."""

    async def __aenter__(self):
        raise Boom("unexpected")

    async def __aexit__(self, *exc):
        return False


async def test_a_synthetic_check_that_cannot_start_playwright_is_logged(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(synthetic_mod, "async_playwright", _NoBrowser)
    check = SyntheticCheck(
        _cfg("synthetic", "https://site.example.test", script_code="pass")
    )

    with caplog.at_level(logging.DEBUG):
        result = await check.run()

    assert result["success"] is False
    logged = [r for r in caplog.records if r.exc_info and r.exc_info[0] is Boom]
    assert [r.levelno for r in logged] == [logging.DEBUG], caplog.text


def _tracebacks(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Every record that carries a traceback, at any level."""
    return [r for r in caplog.records if r.exc_info]


async def _replying(reply: bytes):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.read(100)
            writer.write(reply)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


EXPECTED_FAILURES = {
    "postgres refused": lambda port: PostgreSQLCheck(
        _cfg("postgres", "postgresql://u:s3cr3t@127.0.0.1:1/db")
    ),
    "postgres unresolvable": lambda port: PostgreSQLCheck(
        _cfg("postgres", "postgresql://u:s3cr3t@no-such-host.invalid:5432/db")
    ),
    "tcp reply mismatch": lambda port: TCPCheck(
        _cfg("tcp", "127.0.0.1", port=port, send_string="PING", expect_string="PONG")
    ),
    "dns nameserver by name": lambda port: DNSCheck(
        _cfg("dns", "example.test", nameserver="localhost", record_type="A")
    ),
    "http port out of range": lambda port: HTTPCheck(
        _cfg("http", "http://127.0.0.1:99999/health")
    ),
    "http host IDNA cannot encode": lambda port: HTTPCheck(
        _cfg("http", "http://xn--a.example/")
    ),
    "http host IDNA forbids": lambda port: HTTPCheck(
        _cfg("http", "http://\u2603.example/")
    ),
    "http target with a trailing newline": lambda port: HTTPCheck(
        _cfg("http", "http://127.0.0.1:1/health\n")
    ),
    "json target with a tab": lambda port: JSONCheck(
        _cfg("json", "http://127.0.0.1:1/a\tb", json_path="a", expected_value="b")
    ),
    "tcp host label too long": lambda port: TCPCheck(
        _cfg("tcp", "a" * 64 + ".example", port=80)
    ),
    "http target blocked": lambda port: HTTPCheck(
        _cfg("http", "http://169.254.169.254/latest/meta-data/")
    ),
    "json port out of range": lambda port: JSONCheck(
        _cfg("json", "http://127.0.0.1:99999/", json_path="a", expected_value="b")
    ),
    "postgres settings refused": lambda port: PostgreSQLCheck(
        _cfg("postgres", "postgresql://u:s3cr3t@127.0.0.1:1/db?sslmode=bogus")
    ),
    "json target blocked": lambda port: JSONCheck(
        _cfg("json", "http://169.254.169.254/", json_path="a", expected_value="b")
    ),
}


@pytest.mark.parametrize(
    "make_check", EXPECTED_FAILURES.values(), ids=list(EXPECTED_FAILURES)
)
async def test_an_expected_failure_is_a_result_with_no_traceback(
    caplog, make_check
) -> None:
    server, port = await _replying(b"NOPE\n")
    try:
        with caplog.at_level(logging.DEBUG):
            result = await make_check(port).run()
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()

    assert result["success"] is False
    assert result["error"]
    assert not _tracebacks(caplog), [r.getMessage() for r in _tracebacks(caplog)]


async def test_a_matching_tcp_reply_passes_unlogged(caplog) -> None:
    server, port = await _replying(b"PONG\n")
    try:
        check = TCPCheck(
            _cfg(
                "tcp", "127.0.0.1", port=port, send_string="PING", expect_string="PONG"
            )
        )
        with caplog.at_level(logging.DEBUG):
            result = await check.run()
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()

    assert result["success"] is True, result
    assert not _tracebacks(caplog)


async def test_a_defect_in_the_http_send_path_is_logged(monkeypatch, caplog) -> None:
    async def broken(*args, **kwargs):
        raise TypeError("a defect, not a setting")

    monkeypatch.setattr(http_mod, "ssrf_guarded_send", broken)

    with caplog.at_level(logging.DEBUG):
        result = await HTTPCheck(_cfg("http", "http://127.0.0.1:1/")).run()

    assert result["success"] is False
    assert [r.exc_info[0] for r in caplog.records if r.exc_info] == [TypeError], (
        caplog.text
    )


async def test_a_failed_echo_closes_the_connection(caplog) -> None:
    saw_eof = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.read(100)
            writer.write(b"NOPE\n")
            await writer.drain()
            if await reader.read(100) == b"":
                saw_eof.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        check = TCPCheck(
            _cfg(
                "tcp",
                "127.0.0.1",
                port=port,
                send_string="PING",
                expect_string="PONG",
                timeout=2,
            )
        )
        with caplog.at_level(logging.DEBUG):
            result = await check.run()
        await asyncio.wait_for(saw_eof.wait(), 2)
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()

    assert result["success"] is False
    assert not _tracebacks(caplog)


def _mysql_greeting() -> bytes:
    """A MySQL 8 server greeting offering mysql_native_password."""
    caps = 0x00000200 | 0x00008000 | 0x00080000 | 0x00000008 | 0x00000001
    payload = (
        b"\x0a8.0.36\0"
        + struct.pack("<I", 7)
        + b"abcdefgh\0"
        + struct.pack("<H", caps & 0xFFFF)
        + b"\x21"
        + struct.pack("<H", 2)
        + struct.pack("<H", caps >> 16)
        + bytes([21])
        + b"\0" * 10
        + b"ijklmnopqrst\0"
        + b"mysql_native_password\0"
    )
    return struct.pack("<I", len(payload))[:3] + b"\x00" + payload


@pytest.mark.parametrize(
    "password", ["S3cretPW", "S\u20accretPW"], ids=["ascii", "euro"]
)
async def test_a_mysql_password_its_login_cannot_encode_is_quoted_nowhere(
    caplog, password: str
) -> None:
    pytest.importorskip("aiomysql")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            writer.write(_mysql_greeting())
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), 2)
        except TimeoutError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        check = MySQLCheck(_cfg("mysql", f"mysql://u:{password}@127.0.0.1:{port}/db"))
        with caplog.at_level(logging.DEBUG):
            result = await check.run()
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()

    assert result["success"] is False
    assert not _tracebacks(caplog), result["error"]
    assert "\u20ac" not in caplog.text + (result["error"] or "")
