"""A PostgreSQL login the agent cannot complete names its cause.

asyncpg reports a failure inside its login exchange as an
``InternalClientError`` whose cause is the real error, and a login method it
does not support as an ``InterfaceError`` before any connection exists. The
check reports each by its cause: a method the agent cannot perform is a
``connection_error``, a password login with no password an
``authentication_error``; neither is an unexpected error or a query error.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from uuid import uuid4

import pytest
from app.checks.postgres import PostgreSQLCheck

pytestmark = pytest.mark.unit

pytest.importorskip("asyncpg")

AUTH_CLEARTEXT = 3
AUTH_SCM_CREDENTIAL = 6
AUTH_GSS = 7


async def _server_asking_for(auth_code: int):
    """A server that answers the startup message with one authentication request."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            (length,) = struct.unpack("!I", await reader.readexactly(4))
            await reader.readexactly(length - 4)
            writer.write(b"R" + struct.pack("!II", 8, auth_code))
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), 2)
        except asyncio.IncompleteReadError, TimeoutError, ConnectionError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _run(monkeypatch, tmp_path, auth_code: int, userinfo: str) -> dict:
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "no-pgpass"))
    server, port = await _server_asking_for(auth_code)
    try:
        check = PostgreSQLCheck(
            {
                "check_id": str(uuid4()),
                "name": "pg-login",
                "check_type": "postgres",
                "target": f"postgresql://{userinfo}@127.0.0.1:{port}/db?sslmode=disable",
                "retries": 1,
                "timeout": 3,
            }
        )
        return await check.run()
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("auth_code", "userinfo", "error_type", "error_start"),
    [
        (AUTH_GSS, "u:pw", "connection_error", "Login method unavailable: "),
        (
            AUTH_CLEARTEXT,
            "u",
            "authentication_error",
            "Authentication failed - the server asked for a password",
        ),
        (
            AUTH_SCM_CREDENTIAL,
            "u:pw",
            "connection_error",
            "Login method unavailable: ",
        ),
    ],
    ids=["gss without gssapi", "cleartext with no password", "unsupported method"],
)
async def test_a_login_the_agent_cannot_complete_names_its_cause(
    monkeypatch, tmp_path, caplog, auth_code, userinfo, error_type, error_start
) -> None:
    with caplog.at_level(logging.DEBUG):
        result = await _run(monkeypatch, tmp_path, auth_code, userinfo)

    assert result["success"] is False
    assert result["metrics"]["postgres"]["error_type"] == error_type, result["error"]
    assert result["error"].startswith(error_start), result["error"]
    assert not [r for r in caplog.records if r.exc_info], caplog.text
