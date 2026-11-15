"""A synthetic run that fails reports the class of the exception that failed it.

The server shows a reader who is not shown the script (a viewer) that class name
alone, so it is a field of its own (`metrics.synthetic.error_type`), set by the
agent after the script's own fields: a script cannot name it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from app.checks import synthetic as synthetic_mod
from app.checks.synthetic import SyntheticCheck

pytestmark = pytest.mark.unit


class _Tracing:
    async def start(self, **kwargs) -> None:
        return None

    async def stop(self, path: str) -> None:
        await asyncio.to_thread(Path(path).write_bytes, b"trace")


class _Page:
    def on(self, event, handler) -> None:
        return None

    async def evaluate(self, expression):
        return {}

    async def screenshot(self, **kwargs) -> bytes:
        return b"png"


class _Context:
    tracing = _Tracing()

    async def new_page(self) -> _Page:
        return _Page()


class _Browser:
    async def new_context(self) -> _Context:
        return _Context()

    async def close(self) -> None:
        return None


class _Chromium:
    async def launch(self, headless: bool) -> _Browser:
        return _Browser()


class _Playwright:
    """`async_playwright()` with a browser that opens no page."""

    chromium = _Chromium()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _run(monkeypatch, script: str) -> dict:
    monkeypatch.setattr(synthetic_mod, "async_playwright", _Playwright)
    check = SyntheticCheck(
        {
            "check_id": str(uuid4()),
            "name": "synthetic-error-type",
            "check_type": "synthetic",
            "target": "https://site.example.test",
            "script_code": script,
        }
    )
    return await check.run()


async def test_a_script_that_raises_reports_the_exception_class(monkeypatch) -> None:
    result = await _run(
        monkeypatch, "async def run_check(page):\n    raise ValueError('bad')\n"
    )

    assert result["success"] is False
    assert result["metrics"]["synthetic"]["error_type"] == "ValueError"
    assert result["error"].startswith("Script execution failed: ValueError: bad")


async def test_a_script_that_does_not_compile_reports_the_exception_class(
    monkeypatch,
) -> None:
    result = await _run(monkeypatch, "async def run_check(page):\n  return (\n")

    assert result["metrics"]["synthetic"]["error_type"] == "SyntaxError"
    assert result["error"].startswith("Script compilation failed: SyntaxError: ")


async def test_a_script_cannot_name_the_exception_class(monkeypatch) -> None:
    result = await _run(
        monkeypatch,
        "async def run_check(page):\n"
        "    return {'status': 'failure', 'error_type': 'CorrectHorseError'}\n",
    )

    assert result["success"] is False
    assert result["metrics"]["synthetic"]["error_type"] is None


async def test_a_passing_run_reports_no_exception_class(monkeypatch) -> None:
    result = await _run(
        monkeypatch, "async def run_check(page):\n    return {'status': 'success'}\n"
    )

    assert result["success"] is True
    assert result["metrics"]["synthetic"]["error_type"] is None
