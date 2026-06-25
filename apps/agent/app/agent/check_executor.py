"""
Agent check execution management.

Handles the execution of individual checks including:
- Concurrency control via semaphore
- Timeout handling
- Metrics tracking (success/failure rates)
- Result queue management
"""

import asyncio
import time

from shared.logger import get_logger
from uuid_extensions import uuid7

from app.checks.base import BaseCheck

logger = get_logger("luxswirl.agent.check_executor")


class CheckExecutor:
    """Manages execution of individual checks with concurrency control."""

    def __init__(
        self,
        config: dict,
        metrics: dict,
        check_stats: dict,
        semaphore: asyncio.Semaphore,
        result_queue: asyncio.Queue,
        last_state: dict,
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
        Run a single check and collect the result.

        Args:
            check: The check to run
        """
        check_timeout = check.config.get("timeout", 10)
        self.logger.debug(
            "Running check",
            extra={"check_name": check.name},
        )

        # Generate unique result ID before execution (for artifact linking)
        # Use UUIDv7 (time-ordered) for better database performance with time-series data
        result_id = uuid7()

        try:
            async with self.semaphore:
                result = await asyncio.wait_for(check.run(), timeout=check_timeout)

                # Update internal metrics
                self.metrics["checks_executed"] += 1
                if result["success"]:
                    self.metrics["checks_succeeded"] += 1
                    self.check_stats[check.name]["successes"] += 1
                else:
                    self.metrics["checks_failed"] += 1
                    self.check_stats[check.name]["failures"] += 1

                # Update check stats
                self.check_stats[check.name]["total_runs"] += 1
                if result.get("latency_ms"):
                    self.check_stats[check.name]["total_latency"] += result["latency_ms"]

                # Add agent metadata to the result
                agent_id = self.config.get("agent_id", "unknown")
                result["result_id"] = str(result_id)  # UUID for artifact linking
                result["agent_id"] = str(agent_id) if agent_id != "unknown" else "unknown"
                result["agent_run_id"] = self.run_id
                result["timestamp"] = time.time()

                # Store the last state
                self.last_state[check.name] = result

                # Put in queue for reporting
                await self.result_queue.put(result)

                # Track maximum queue size
                if self.result_queue.qsize() > self.metrics["result_queue_max_size"]:
                    self.metrics["result_queue_max_size"] = self.result_queue.qsize()

        except TimeoutError:
            self.logger.warning(
                "Check timed out",
                extra={"check_name": check.name},
            )
            self.metrics["checks_executed"] += 1
            self.metrics["checks_failed"] += 1
            self.check_stats[check.name]["total_runs"] += 1
            self.check_stats[check.name]["failures"] += 1

            # Create timeout result
            agent_id = self.config.get("agent_id", "unknown")
            result = {
                "check_id": str(check.check_id),
                "display_name": check.name,
                "check_type": check.config["check_type"],
                "target": check.config["target"],
                "success": False,
                "latency_ms": None,
                "error": "timeout",
                "agent_id": str(agent_id) if agent_id != "unknown" else "unknown",
                "agent_run_id": self.run_id,
                "timestamp": time.time(),
            }

            self.last_state[check.name] = result
            await self.result_queue.put(result)

        except Exception as e:
            self.logger.error(
                "Check failed with exception",
                extra={"check_name": check.name},
                exc_info=True,
            )
            self.metrics["checks_executed"] += 1
            self.metrics["checks_failed"] += 1
            self.check_stats[check.name]["total_runs"] += 1
            self.check_stats[check.name]["failures"] += 1

            # Create error result
            agent_id = self.config.get("agent_id", "unknown")
            result = {
                "check_id": str(check.check_id),
                "display_name": check.name,
                "check_type": check.config["check_type"],
                "target": check.config["target"],
                "success": False,
                "latency_ms": None,
                "error": str(e),
                "agent_id": str(agent_id) if agent_id != "unknown" else "unknown",
                "agent_run_id": self.run_id,
                "timestamp": time.time(),
            }

            self.last_state[check.name] = result
            await self.result_queue.put(result)
