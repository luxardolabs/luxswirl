"""The server's check fields reach the check that runs them.

`_transform_check_config` rewrote the server's `http_method` into a key no check
reads (they read `method`), so every HTTP/JSON check sent GET whatever the form
said.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from app.agent.check_manager import CheckManager
from app.checks import http as http_check
from app.checks.http import HTTPCheck

pytestmark = pytest.mark.unit


def _transform(server_check: dict[str, Any]) -> dict[str, Any]:
    manager = CheckManager(
        config={},
        cache_file=Path("/nonexistent"),
        credentials=None,
        on_orphaned_agent=None,
    )
    return manager._transform_check_config(server_check, "http", "c1")


def _server_check(**fields: Any) -> dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "display_name": "c1",
        "target": "https://example.test/health",
        "interval_seconds": 60,
        "timeout_seconds": 5,
        "enabled": True,
        **fields,
    }


async def test_the_configured_verb_reaches_the_http_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    async def refuse(client: httpx.AsyncClient, method: str, *_: Any, **__: Any):
        sent.append(method)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(http_check, "ssrf_guarded_send", refuse)
    check = HTTPCheck(_transform(_server_check(http_method="POST")))

    await check.run()

    assert sent == ["POST"]


def test_the_retry_settings_reach_the_executor() -> None:
    cfg = _transform(_server_check(retry_attempts=3, retry_interval_seconds=15))

    assert (cfg["retry_attempts"], cfg["retry_interval"]) == (3, 15)


def test_a_check_with_no_retry_settings_is_tried_once() -> None:
    cfg = _transform(_server_check())

    assert (cfg["retry_attempts"], cfg["retry_interval"]) == (0, 30)
