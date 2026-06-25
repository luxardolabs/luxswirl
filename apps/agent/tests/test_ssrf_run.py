"""SSRF wiring tests — every check refuses a target that resolves into the cloud
metadata range, before opening any connection.

``169.254.169.254`` is a literal IP (no DNS lookup), so these run fully offline.
http/json/tcp surface the block as a failed result (their retry/except path turns
the raise into a result); ping/dns/db let ``SsrfBlockedError`` propagate out of
``run()`` (the agent's check executor turns that into a failure in production)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from shared.ssrf import SsrfBlockedError

from app.checks.dns import DNSCheck
from app.checks.http import HTTPCheck
from app.checks.json import JSONCheck
from app.checks.mysql import MySQLCheck
from app.checks.ping import PingCheck
from app.checks.postgres import PostgreSQLCheck
from app.checks.tcp import TCPCheck

pytestmark = pytest.mark.pure

METADATA_IP = "169.254.169.254"


def _cfg(**overrides):
    cfg = {"check_id": str(uuid4()), "name": "ssrf-test", "check_type": "ping", "retries": 1}
    cfg.update(overrides)
    return cfg


async def _assert_blocked(check):
    """The check must block the metadata target — by raising SsrfBlockedError or by
    returning a failed result that names the blocked range."""
    try:
        result = await check.run()
    except SsrfBlockedError:
        return
    assert result["success"] is False
    err = result.get("error") or ""
    assert "169.254" in err or "metadata" in err.lower(), err


async def test_http_blocks_metadata():
    await _assert_blocked(
        HTTPCheck(_cfg(check_type="http", target=f"http://{METADATA_IP}/latest/meta-data/"))
    )


async def test_json_blocks_metadata():
    await _assert_blocked(
        JSONCheck(
            _cfg(
                check_type="json",
                target=f"http://{METADATA_IP}/",
                json_path="status",
                expected_value="ok",
            )
        )
    )


async def test_tcp_blocks_metadata():
    await _assert_blocked(TCPCheck(_cfg(check_type="tcp", target=METADATA_IP, port=80)))


async def test_ping_blocks_metadata():
    await _assert_blocked(PingCheck(_cfg(check_type="ping", target=METADATA_IP)))


async def test_dns_blocks_metadata_nameserver():
    pytest.importorskip("dns.asyncresolver")
    await _assert_blocked(
        DNSCheck(
            _cfg(check_type="dns", target="example.com", record_type="A", nameserver=METADATA_IP)
        )
    )


async def test_mysql_blocks_metadata():
    pytest.importorskip("aiomysql")
    # The connection string lives in `target` (BaseCheck requires that field).
    await _assert_blocked(
        MySQLCheck(_cfg(check_type="mysql", target=f"mysql://u:p@{METADATA_IP}:3306/db"))
    )


async def test_postgres_blocks_metadata():
    pytest.importorskip("asyncpg")
    await _assert_blocked(
        PostgreSQLCheck(
            _cfg(check_type="postgres", target=f"postgresql://u:p@{METADATA_IP}:5432/db")
        )
    )
