"""`python -m app.main --validate-only`: the agent's effective config (the
defaults with the environment, overlaid by a `--config` file) is held to
`validate_config` and to the server-address rules the agent starts under, with
or without a file. It exits 1 naming what is wrong, else 0, and never starts the
agent. Without the flag, a config that fails the same validation stops startup.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from app import main as agent_main
from app.shared import config as shared_config

pytestmark = pytest.mark.unit


class _AgentStarted(Exception):
    """Raised by the stand-in agent: the run got past validation."""


class _FakeAgent:
    started: list[dict[str, Any]] = []

    def __init__(self, config: dict[str, Any]) -> None:
        _FakeAgent.started.append(config)
        raise _AgentStarted


@pytest.fixture(autouse=True)
def _no_real_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAgent.started = []
    monkeypatch.setattr(agent_main, "LuxSwirlAgent", _FakeAgent)
    monkeypatch.setitem(
        shared_config.DEFAULT_AGENT_CONFIG, "server_url", "http://luxswirl_server:9000"
    )


async def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["app.main", *argv])
    return await agent_main.main()


def _file(tmp_path: Path, body: dict[str, Any]) -> str:
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(body))
    return str(path)


def _errors(caplog: pytest.LogCaptureFixture) -> str:
    """Every `error` a log record carries in its extra fields."""
    return " ".join(str(getattr(r, "error", "")) for r in caplog.records)


async def test_validate_only_passes_the_defaults_and_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _run(monkeypatch, "--validate-only") == 0
    assert _FakeAgent.started == []


async def test_validate_only_refuses_an_external_plain_http_server_from_the_env(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(
        shared_config.DEFAULT_AGENT_CONFIG, "server_url", "http://monitor.example.com"
    )
    monkeypatch.delenv("LUXSWIRL_ALLOW_INSECURE_HTTP", raising=False)
    caplog.set_level(logging.INFO)

    assert await _run(monkeypatch, "--validate-only") == 1
    assert "monitor.example.com" in _errors(caplog)
    assert _FakeAgent.started == []


async def test_validate_only_with_a_valid_file_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _file(tmp_path, {"agent_name": "edge-1", "interval": 30})

    assert await _run(monkeypatch, "--validate-only", "--config", path) == 0
    assert _FakeAgent.started == []


async def test_validate_only_names_a_duplicate_check_in_the_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    check = {"name": "web", "check_type": "ping", "target": "10.0.0.1"}
    path = _file(tmp_path, {"checks": [check, check]})
    caplog.set_level(logging.INFO)

    assert await _run(monkeypatch, "--validate-only", "--config", path) == 1
    assert "Duplicate check names: web" in caplog.text
    assert _FakeAgent.started == []


async def test_validate_only_refuses_a_server_url_with_a_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _file(tmp_path, {"server_url": "http://luxswirl_server:9000/api/v1"})
    caplog.set_level(logging.INFO)

    assert await _run(monkeypatch, "--validate-only", "--config", path) == 1
    assert "no path" in _errors(caplog)
    assert _FakeAgent.started == []


async def test_validate_only_refuses_a_file_that_is_not_a_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent.json"
    path.write_text("[1, 2]")

    assert await _run(monkeypatch, "--validate-only", "--config", str(path)) == 1


async def test_validate_only_refuses_a_file_that_does_not_parse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent.json"
    path.write_text("{not json")

    assert await _run(monkeypatch, "--validate-only", "--config", str(path)) == 1


async def test_a_file_runs_over_the_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _file(tmp_path, {"interval": 30})

    assert await _run(monkeypatch, "--config", path) == 1  # the stand-in raises
    (config,) = _FakeAgent.started
    assert config["interval"] == 30
    assert config["server_url"] == "http://luxswirl_server:9000"
    assert config["report_batch_size"] == 5000


async def test_startup_stops_on_an_invalid_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _file(tmp_path, {"checks": "ping everything"})

    assert await _run(monkeypatch, "--config", path) == 1
    assert _FakeAgent.started == []
