"""
Agent check execution management.

Handles the execution of individual checks including:
- Concurrency control via semaphore
- Timeout handling, per attempt
- Retries of a failing check, reported as one run
- Metrics tracking (success/failure rates)
- Result queue management
"""

import asyncio
import time
from typing import Any

from uuid_extensions import uuid7

from app.checks.base import BaseCheck
from app.shared.logger import get_logger

logger = get_logger("luxswirl.agent.check_executor")


class CheckExecutor:
    """Manages execution of individual checks with concurrency control."""

    def __init__(
        self,
        config: dict[str, Any],
        metrics: dict[str, Any],
        check_stats: dict[str, Any],
        semaphore: asyncio.Semaphore,
        result_queue: asyncio.Queue[dict[str, Any]],
        last_state: dict[str, Any],
        run_id: str,
    ):
        """
        Initialize check executor.

        Args:
            config: Agent configuration
            metrics: Agent metrics dict
            check_stats: Check statistics dict
            semaphore: Concurrency control semaphore
            result_queue: Queue for results
            last_state: Dict tracking last check states
            run_id: Unique run ID for this agent instance
        """
        self.config = config
        self.metrics = metrics
        self.check_stats = check_stats
        self.semaphore = semaphore
        self.result_queue = result_queue
        self.last_state = last_state
        self.run_id = run_id
        self.logger = logger

    async def run_check(self, check: BaseCheck) -> None:
        """
        Run a check, retrying a failure, and report the run once.

        A failed attempt is tried again up to `retry_attempts` times,
        `retry_interval` seconds apart, while the next attempt can still finish
        before the check's next scheduled run (`interval`): an attempt is expected
        to last its timeout, or as long as the longest one so far (a cancelled
        check's cleanup counts), and that is checked again once it has a slot.
        `timeout` applies to each attempt. The run reports its last attempt's
        result once, stamped with the time the run started, with the number of
        attempts in `metrics.attempts`: a check that recovers on a retry is one
        success. The wait between attempts holds no concurrency slot.

        Args:
            check: The check to run
        """
        timeout = float(check.config.get("timeout") or 10)
        retries = max(0, int(check.config.get("retry_attempts") or 0))
        retry_interval = float(check.config.get("retry_interval", 30))
        started_at = time.time()
        next_run = time.monotonic() + float(check.config.get("interval") or 60)
        self.logger.debug(
            "Running check",
            extra={"check_name": check.name},
        )

        # Generate unique result ID before execution (for artifact linking)
        # Use UUIDv7 (time-ordered) for better database performance with time-series data
        result_id = uuid7()

        attempts = 1
        longest = 0.0
        async with self.semaphore:
            result, longest = await self._run(check, timeout, longest)
        while not result["success"] and attempts <= retries:
            if time.monotonic() + retry_interval + longest > next_run:
                break
            await asyncio.sleep(retry_interval)
            retried, longest = await self._attempt(check, timeout, longest, next_run)
            if retried is None:  # its slot came too late to finish before next_run
                break
            result = retried
            attempts += 1

        metrics = result.get("metrics")
        result["metrics"] = {
            **(metrics if isinstance(metrics, dict) else {}),
            "attempts": attempts,
        }
        await self._record(check, result, str(result_id), started_at)

    async def _attempt(
        self,
        check: BaseCheck,
        timeout: float,
        longest: float,
        finish_by: float,
    ) -> tuple[dict[str, Any] | None, float]:
        """One retry of the check inside a concurrency slot.

        Returns the result (None when, once in its slot, an attempt expected to
        last `longest` could not finish by `finish_by`) and the longest attempt so
        far, this one included.
        """
        async with self.semaphore:
            if time.monotonic() + longest > finish_by:
                return None, longest
            return await self._run(check, timeout, longest)

    async def _run(
        self, check: BaseCheck, timeout: float, longest: float
    ) -> tuple[dict[str, Any], float]:
        """Run the check once, in a slot the caller holds; a failure is a result.

        Returns the result and the longest attempt so far, this one included.
        """
        begun = time.monotonic()
        try:
            result = await asyncio.wait_for(check.run(), timeout=timeout)
        except TimeoutError:
            self.logger.warning(
                "Check timed out",
                extra={"check_name": check.name},
            )
            result = self._failure(check, "timeout")
        except Exception as e:
            self.logger.exception(
                "Check failed with exception",
                extra={"check_name": check.name},
            )
            result = self._failure(check, str(e))
        # wait_for returns after a cancelled check's cleanup, so this is
        # how long an attempt really holds the check.
        return result, max(longest, timeout, time.monotonic() - begun)

    @staticmethod
    def _failure(check: BaseCheck, error: str) -> dict[str, Any]:
        return {
            "check_id": str(check.check_id),
            "display_name": check.name,
            "check_type": check.config["check_type"],
            "target": check.config["target"],
            "success": False,
            "latency_ms": None,
            "error": error,
        }

    async def _record(
        self,
        check: BaseCheck,
        result: dict[str, Any],
        result_id: str,
        started_at: float,
    ) -> None:
        """Count the run, stamp the agent's metadata, and queue it for reporting."""
        self.metrics["checks_executed"] += 1
        if result["success"]:
            self.metrics["checks_succeeded"] += 1
            self.check_stats[check.name]["successes"] += 1
        else:
            self.metrics["checks_failed"] += 1
            self.check_stats[check.name]["failures"] += 1

        self.check_stats[check.name]["total_runs"] += 1
        if result.get("latency_ms"):
            self.check_stats[check.name]["total_latency"] += result["latency_ms"]

        agent_id = self.config.get("agent_id", "unknown")
        result["result_id"] = result_id  # UUID for artifact linking
        result["agent_id"] = str(agent_id) if agent_id != "unknown" else "unknown"
        result["agent_run_id"] = self.run_id
        result["timestamp"] = started_at

        self.last_state[check.name] = result
        await self.result_queue.put(result)

        if self.result_queue.qsize() > self.metrics["result_queue_max_size"]:
            self.metrics["result_queue_max_size"] = self.result_queue.qsize()
