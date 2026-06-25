"""
Safe subprocess execution utilities.

This module provides subprocess execution with guaranteed cleanup to prevent
resource leaks (file descriptors, zombie processes).

SWIRL-57: Subprocess resource leak fix
- All subprocess calls must use timeouts with grace periods
- Processes must be terminated and reaped on timeout/error
- No subprocess should outlive its parent task
"""

import asyncio
from typing import Any

from shared.logger import get_logger

logger = get_logger("luxswirl.subprocess_safe")


async def run_subprocess_safely(
    *args,
    timeout: float,
    capture_output: bool = True,
    grace_seconds: float = 2.0,
    kill_timeout: float = 5.0,
) -> tuple[int | None, bytes | None, bytes | None]:
    """
    Run a subprocess with guaranteed cleanup on timeout or error.

    This function ensures that subprocesses are properly terminated and reaped,
    preventing file descriptor leaks and zombie processes.

    Args:
        *args: Command and arguments to execute
        timeout: Maximum time to wait for subprocess completion (seconds)
        capture_output: Whether to capture stdout/stderr (default: True)
        grace_seconds: Extra time beyond timeout before forcing kill (default: 2.0)
        kill_timeout: Max time to wait for process termination after kill (default: 5.0)

    Returns:
        Tuple of (returncode, stdout, stderr)
        - returncode: Process exit code, or None if killed
        - stdout: Captured stdout bytes, or None if not captured
        - stderr: Captured stderr bytes, or None if not captured

    Raises:
        TimeoutError: If subprocess exceeds timeout + grace period
        OSError: If subprocess execution fails

    Example:
        >>> returncode, stdout, stderr = await run_subprocess_safely(
        ...     "ping", "-c", "1", "example.com",
        ...     timeout=5.0
        ... )
    """
    proc = None
    try:
        # Create subprocess with appropriate pipes
        stdout_pipe = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
        stderr_pipe = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=stdout_pipe,
            stderr=stderr_pipe,
        )

        logger.debug(
            "Subprocess started",
            extra={
                "command_args": " ".join(str(a) for a in args),
                "pid": proc.pid,
                "timeout_seconds": timeout,
            },
        )

        # Wait for process with timeout
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout + grace_seconds,
            )

            logger.debug(
                "Subprocess completed",
                extra={"pid": proc.pid, "returncode": proc.returncode},
            )
            return proc.returncode, stdout, stderr

        except TimeoutError:
            logger.warning(
                "Subprocess timeout",
                extra={
                    "timeout_seconds": timeout + grace_seconds,
                    "command_args": " ".join(str(a) for a in args),
                    "pid": proc.pid,
                },
            )

            # Try graceful termination first
            if proc.returncode is None:
                logger.debug("Terminating subprocess", extra={"pid": proc.pid})
                proc.terminate()

                # Give it a moment to die gracefully
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                    logger.debug(
                        "Subprocess terminated gracefully",
                        extra={"pid": proc.pid},
                    )
                except TimeoutError:
                    # Force kill if it doesn't respond to SIGTERM
                    logger.warning(
                        "Subprocess didn't respond to SIGTERM, force killing",
                        extra={"pid": proc.pid},
                    )
                    proc.kill()

                    # Wait for it to die
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
                        logger.debug("Subprocess killed", extra={"pid": proc.pid})
                    except TimeoutError:
                        logger.error(
                            "Failed to kill subprocess even with SIGKILL - "
                            "this may indicate a kernel/system issue",
                            extra={"pid": proc.pid},
                        )

            # Re-raise timeout
            raise TimeoutError(
                f"Subprocess exceeded timeout of {timeout}s (grace: {grace_seconds}s)"
            ) from None

    except Exception:
        # Ensure cleanup on any error
        if proc and proc.returncode is None:
            logger.warning(
                "Exception during subprocess execution, cleaning up",
                extra={"pid": proc.pid},
                exc_info=True,
            )
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
            except Exception:
                logger.error(
                    "Failed to cleanup subprocess",
                    extra={"pid": proc.pid},
                    exc_info=True,
                )
        raise


async def run_subprocess_no_output(
    *args,
    timeout: float,
    grace_seconds: float = 2.0,
    kill_timeout: float = 5.0,
) -> int | None:
    """
    Run a subprocess without capturing output (stdout/stderr to DEVNULL).

    Convenience wrapper around run_subprocess_safely() for cases where
    output is not needed. Useful for network scans and pings.

    Args:
        *args: Command and arguments to execute
        timeout: Maximum time to wait for subprocess completion (seconds)
        grace_seconds: Extra time beyond timeout before forcing kill (default: 2.0)
        kill_timeout: Max time to wait for process termination after kill (default: 5.0)

    Returns:
        Process exit code, or None if killed

    Raises:
        TimeoutError: If subprocess exceeds timeout + grace period
        OSError: If subprocess execution fails

    Example:
        >>> returncode = await run_subprocess_no_output(
        ...     "ping", "-c", "1", "example.com",
        ...     timeout=5.0
        ... )
        >>> if returncode == 0:
        ...     print("Ping succeeded!")
    """
    returncode, _, _ = await run_subprocess_safely(
        *args,
        timeout=timeout,
        capture_output=False,
        grace_seconds=grace_seconds,
        kill_timeout=kill_timeout,
    )
    return returncode


def get_subprocess_config(config: dict[str, Any]) -> dict[str, float]:
    """
    Extract subprocess-related configuration values.

    Args:
        config: Agent configuration dictionary

    Returns:
        Dictionary with subprocess config:
        - grace_seconds: Grace period before killing
        - kill_timeout: Max wait time for termination
    """
    return {
        "grace_seconds": config.get("subprocess_timeout_grace_seconds", 2.0),
        "kill_timeout": config.get("subprocess_kill_timeout_seconds", 5.0),
    }
