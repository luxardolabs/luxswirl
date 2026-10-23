"""Every subprocess the agent starts ends with it: it returns its exit code and
output, and one that outlives its timeout plus grace is terminated, killed if it
ignores SIGTERM, reaped, and reported as a TimeoutError. None is left running.

These run real short-lived processes (sh, sleep) from the image.
"""

from __future__ import annotations

import os
import time

import pytest
from app.shared.subprocess_safe import (
    run_subprocess_no_output,
    run_subprocess_safely,
)

pytestmark = pytest.mark.unit


def _alive(pid: int) -> bool:
    """Whether `pid` is still a process (a reaped one is gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_a_process_returns_its_exit_code_and_output() -> None:
    code, out, err = await run_subprocess_safely(
        "sh", "-c", "echo out; echo err >&2; exit 3", timeout=5
    )

    assert code == 3
    assert out == b"out\n"
    assert err == b"err\n"


async def test_without_capture_only_the_exit_code_comes_back() -> None:
    code, out, err = await run_subprocess_safely(
        "sh", "-c", "echo noisy", timeout=5, capture_output=False
    )

    assert (code, out, err) == (0, None, None)
    assert await run_subprocess_no_output("sh", "-c", "exit 7", timeout=5) == 7


async def test_a_process_past_its_timeout_is_terminated_and_reaped() -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timeout of 0.2s"):
        await run_subprocess_safely(
            "sh", "-c", "echo $$; exec sleep 30", timeout=0.2, grace_seconds=0.1
        )

    assert time.monotonic() - started < 5


async def test_a_process_that_ignores_sigterm_is_killed() -> None:
    """`trap '' TERM` makes the shell ignore SIGTERM, so only SIGKILL ends it."""
    script = "trap '' TERM; echo ready; while :; do sleep 0.05; done"
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await run_subprocess_no_output(
            "sh", "-c", script, timeout=0.2, grace_seconds=0.1, kill_timeout=2
        )

    # terminate (1 s wait) then kill: well under the 30 s it would otherwise run
    assert time.monotonic() - started < 5


async def test_no_process_outlives_a_timeout(tmp_path) -> None:
    pidfile = tmp_path / "pid"
    with pytest.raises(TimeoutError):
        await run_subprocess_safely(
            "sh",
            "-c",
            f"echo $$ > {pidfile}; exec sleep 30",
            timeout=0.3,
            grace_seconds=0.1,
        )

    pid = int(pidfile.read_text())
    assert not _alive(pid), f"pid {pid} still running after the timeout"


async def test_a_command_that_cannot_start_raises() -> None:
    with pytest.raises(OSError):
        await run_subprocess_safely("/nonexistent/luxswirl-binary", timeout=1)
