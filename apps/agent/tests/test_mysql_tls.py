"""A MySQL check that asks for TLS (`?ssl=true`) gets it, or sends nothing.

aiomysql takes an SSL context and, when the server's handshake does not offer TLS,
goes on in plain text with the login. The check refuses that server before it
answers the handshake, and over a server that offers TLS it sends the SSL request
and a TLS ClientHello, never the user name in the clear.

A local stand-in server sends a real MySQL v10 handshake and records every byte the
check sends back.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from typing import Any
from uuid import uuid4

import pytest
from app.checks import _ssrf_policy
from app.checks.base import CheckConfigError
from app.checks.mysql import MySQLCheck

pytest.importorskip("aiomysql")

USER = b"monitor-user"
PASSWORD = "s3cret-pw"
CLIENT_SSL = 0x800
# LONG_PASSWORD | CONNECT_WITH_DB | PROTOCOL_41 | SECURE_CONNECTION | PLUGIN_AUTH
BASE_CAPABILITIES = 0x1 | 0x8 | 0x200 | 0x8000 | 0x80000


def _handshake(capabilities: int) -> bytes:
    payload = (
        b"\x0a"
        + b"8.0.36\x00"
        + struct.pack("<I", 7)
        + b"abcdefgh"
        + b"\x00"
        + struct.pack("<H", capabilities & 0xFFFF)
        + b"\x21"
        + struct.pack("<H", 2)
        + struct.pack("<H", capabilities >> 16)
        + bytes([21])
        + b"\x00" * 10
        + b"ijklmnopqrst\x00"
        + b"mysql_native_password\x00"
    )
    return struct.pack("<I", len(payload))[:3] + b"\x00" + payload


async def _run_against(
    capabilities: int, query_string: str
) -> tuple[dict[str, Any], bytes]:
    received = bytearray()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(_handshake(capabilities))
        await writer.drain()
        answered = False
        with contextlib.suppress(TimeoutError, ConnectionError):
            while chunk := await asyncio.wait_for(reader.read(4096), timeout=1):
                received.extend(chunk)
                # A ClientHello after the SSL request: answer what is no TLS, so
                # the handshake fails as one does.
                if capabilities & CLIENT_SSL and len(received) > 36 and not answered:
                    writer.write(b"not a TLS server\r\n")
                    await writer.drain()
                    answered = True
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    check = MySQLCheck(
        {
            "check_id": str(uuid4()),
            "name": "mysql-tls",
            "check_type": "mysql",
            "target": f"mysql://{USER.decode()}:{PASSWORD}@127.0.0.1:{port}/db"
            + query_string,
            "query": "SELECT 1",
            "timeout": 3,
            "retries": 1,
        }
    )
    async with server:
        result = await check.run()
    return result, bytes(received)


@pytest.fixture(autouse=True)
def _default_policy() -> None:
    _ssrf_policy.apply_server_policy({"block_private_networks": False})


async def test_a_server_offering_no_tls_gets_nothing_back() -> None:
    result, received = await _run_against(BASE_CAPABILITIES, "?ssl=true")

    assert result["success"] is False
    assert "offers no TLS" in result["error"]
    assert received == b""


async def test_without_ssl_the_login_goes_in_the_clear() -> None:
    """The stand-in sees a plain login when TLS is not asked for: what the first test
    shows the check withholding is really there to withhold."""
    result, received = await _run_against(BASE_CAPABILITIES, "")

    assert result["success"] is False
    assert USER in received


@pytest.mark.parametrize("query_string", ["?ssl=false", "?ssl=0"])
async def test_ssl_false_asks_for_no_tls(query_string: str) -> None:
    _, received = await _run_against(BASE_CAPABILITIES, query_string)

    assert USER in received


@pytest.mark.parametrize("query_string", ["?ssl=true", "?ssl=TRUE", "?ssl=1"])
async def test_a_server_offering_tls_gets_a_tls_handshake(query_string: str) -> None:
    result, received = await _run_against(BASE_CAPABILITIES | CLIENT_SSL, query_string)

    length = int.from_bytes(received[:3], "little")
    request = received[4 : 4 + length]
    (flags,) = struct.unpack("<I", request[:4])
    assert length == 32
    assert flags & CLIENT_SSL
    assert received[4 + length : 4 + length + 1] == b"\x16"  # a TLS handshake record
    assert USER not in received
    assert PASSWORD.encode() not in received
    assert result["success"] is False
    assert result["error"].startswith("TLS")


async def test_an_unknown_ssl_value_is_a_config_error() -> None:
    check = MySQLCheck(
        {
            "check_id": str(uuid4()),
            "name": "mysql-tls",
            "check_type": "mysql",
            "target": "mysql://u:p@192.0.2.10:3306/db?ssl=maybe",
            "query": "SELECT 1",
            "timeout": 1,
            "retries": 1,
        }
    )

    with pytest.raises(CheckConfigError, match="ssl=true or ssl=false"):
        await check.run()
