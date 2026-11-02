"""A check whose configuration cannot be used fails as its result, logged once.

`target` and `connection_string` are free text on the server, so a check can
arrive with a host name no resolver can encode (a label over 63 octets), a port
that is not a number, or a PostgreSQL URL naming several hosts. A configuration
the check cannot use is a `CheckConfigError`: the run is a failed result that
says what is wrong, the agent logs it once as a warning (no traceback), and
again only when the error changes. A multi-host PostgreSQL URL is valid for
libpq and asyncpg, so that check runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any
from uuid import uuid4

import pytest
from app.agent.check_executor import CheckExecutor
from app.checks import postgres as postgres_mod
from app.checks.base import BaseCheck, CheckConfigError
from app.checks.mysql import MySQLCheck
from app.checks.ping import PingCheck
from app.checks.postgres import PostgreSQLCheck

pytestmark = pytest.mark.unit

LONG_LABEL = "a" * 64 + ".example.com"
WARNING = "Check configuration invalid"


def _cfg(check_type: str, target: str) -> dict[str, Any]:
    return {
        "check_id": str(uuid4()),
        "name": f"{check_type}-config",
        "check_type": check_type,
        "target": target,
        "retries": 1,
        "timeout": 2,
    }


def _executor() -> tuple[CheckExecutor, asyncio.Queue[dict[str, Any]]]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    stats: defaultdict[str, dict[str, float]] = defaultdict(
        lambda: {"total_runs": 0, "successes": 0, "failures": 0, "total_latency": 0}
    )
    executor = CheckExecutor(
        config={"agent_id": "a1"},
        metrics=defaultdict(int),
        check_stats=stats,
        semaphore=asyncio.Semaphore(5),
        result_queue=queue,
        last_state={},
        run_id="r1",
    )
    return executor, queue


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == WARNING]


BAD_CONFIGS = {
    "ping-long-label": lambda: PingCheck(_cfg("ping", LONG_LABEL)),
    "mysql-long-label": lambda: MySQLCheck(
        _cfg("mysql", f"mysql://u:p@{LONG_LABEL}:3306/db")
    ),
    "mysql-port-out-of-range": lambda: MySQLCheck(
        _cfg("mysql", "mysql://u:p@127.0.0.1:99999/db")
    ),
    "postgres-long-label": lambda: PostgreSQLCheck(
        _cfg("postgres", f"postgresql://u:p@{LONG_LABEL}:5432/db")
    ),
    "postgres-null-in-host": lambda: PostgreSQLCheck(
        _cfg("postgres", "postgresql://u:p@a%00b:5432/db")
    ),
}


@pytest.mark.parametrize("make_check", BAD_CONFIGS.values(), ids=BAD_CONFIGS.keys())
async def test_a_config_error_is_a_failed_result_logged_once(
    caplog: pytest.LogCaptureFixture, make_check: Any
) -> None:
    executor, queue = _executor()
    check = make_check()

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            await executor.run_check(check)

    results = [queue.get_nowait() for _ in range(3)]
    assert [r["success"] for r in results] == [False] * 3
    assert all(r["error"].startswith("Invalid configuration: ") for r in results), (
        results[0]["error"]
    )
    assert not [r for r in caplog.records if r.exc_info], caplog.text
    warned = _warnings(caplog)
    assert [r.levelno for r in warned] == [logging.WARNING], caplog.text


class _Flaky(BaseCheck):
    """Raises the next configuration error, or succeeds once they run out."""

    def __init__(self, config: dict[str, Any], errors: list[str]) -> None:
        super().__init__(config)
        self.errors = errors

    async def run(self) -> dict[str, Any]:
        if self.errors:
            raise CheckConfigError(self.errors.pop(0))
        return self.create_result(success=True, latency_ms=1.0)


async def test_a_changed_or_returning_config_error_is_logged_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor, _ = _executor()
    check = _Flaky(_cfg("http", "http://x"), ["bad port", "bad port", "bad host"])
    with caplog.at_level(logging.WARNING):
        for _ in range(4):  # the fourth run succeeds
            await executor.run_check(check)
        check.errors = ["bad host"]
        await executor.run_check(check)

    assert [r.__dict__["error"] for r in _warnings(caplog)] == [
        "bad port",
        "bad host",
        "bad host",
    ]


async def test_a_multi_host_postgres_url_runs_against_its_hosts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    dsns: list[str] = []

    async def _refused(*, dsn: str, timeout: float) -> None:
        dsns.append(dsn)
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(postgres_mod.asyncpg, "connect", _refused)
    target = "postgresql://u:p@127.0.0.1:5432,127.0.0.2:5433/db"
    executor, queue = _executor()

    with caplog.at_level(logging.DEBUG):
        await executor.run_check(PostgreSQLCheck(_cfg("postgres", target)))

    result = queue.get_nowait()
    assert dsns == [target]
    assert result["success"] is False
    assert result["error"].startswith("Cannot connect to 127.0.0.1,127.0.0.2"), result
    assert result["metrics"]["postgres"]["host"] == "127.0.0.1,127.0.0.2"
    assert result["metrics"]["postgres"]["port"] is None
    assert not [r for r in caplog.records if r.exc_info], caplog.text
