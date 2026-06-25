"""
Agent health monitoring and watchdog.

Handles internal health monitoring including:
- Watchdog for result processing stalls
- Health status reporting
- Internal metrics tracking
- Resource monitoring (file descriptors, subprocesses) - SWIRL-57
"""

import asyncio
import resource
import time
from typing import Any

import psutil
from shared.logger import get_logger

logger = get_logger("luxswirl.agent.health")


class HealthMonitor:
    """Manages agent health monitoring and watchdog functionality."""

    def __init__(
        self,
        config: dict,
        metrics: dict,
        reporter,
        result_queue: asyncio.Queue,
        checks: list,
        watchdog_interval: int = 30,
        watchdog_stall_threshold: int = 3,
        heartbeat_interval: int = 60,
    ):
        """
        Initialize health monitor.

        Args:
            config: Agent configuration
            metrics: Agent metrics dict
            reporter: Reporter instance for flush operations
            result_queue: Result queue to monitor
            checks: List of loaded checks
            watchdog_interval: Seconds between watchdog checks
            watchdog_stall_threshold: Number of stalls before forcing flush
            heartbeat_interval: Heartbeat interval for logging
        """
        self.config = config
        self.metrics = metrics
        self.reporter = reporter
        self.result_queue = result_queue
        self.checks = checks
        self.watchdog_interval = watchdog_interval
        self.watchdog_stall_threshold = watchdog_stall_threshold
        self.heartbeat_interval = heartbeat_interval
        self.logger = logger

        # Control flag (set by parent)
        self.running = False

        # Tracking
        self.last_heartbeat_time = time.time()

        # Resource monitoring (SWIRL-57)
        self.resource_monitoring_enabled = config.get("resource_monitoring_enabled", True)
        self.resource_fd_warning_percent = config.get("resource_fd_warning_percent", 80)
        self.resource_subprocess_warning_count = config.get("resource_subprocess_warning_count", 50)
        self.process = psutil.Process()

    async def monitor_result_processing(self) -> None:
        """Monitor and ensure result processing is working correctly."""
        self.logger.info("Starting watchdog monitor")
        last_queue_size = 0
        stall_count = 0

        while self.running:
            try:
                current_queue_size = self.result_queue.qsize()
                current_time = time.time()

                # Check for processing stalls
                if current_queue_size > 0 and current_queue_size == last_queue_size:
                    stall_count += 1
                    if stall_count >= self.watchdog_stall_threshold:
                        self.logger.warning(
                            "Result processing appears stalled - forcing flush",
                            extra={
                                "queue_size": current_queue_size,
                                "stall_seconds": stall_count * self.watchdog_interval,
                            },
                        )
                        # Force reporter flush
                        try:
                            await self.reporter.flush()
                        except Exception:
                            self.logger.error("Error during forced flush", exc_info=True)
                        stall_count = 0
                else:
                    stall_count = 0

                # Log heartbeat
                if current_time - self.last_heartbeat_time >= self.heartbeat_interval:
                    self.metrics["heartbeats"] += 1
                    self.last_heartbeat_time = current_time
                    active_checks = len([t for t in asyncio.all_tasks() if "run_check" in str(t)])

                    # Get resource metrics for logging
                    resources: dict[str, Any] | None = None
                    if self.resource_monitoring_enabled:
                        resources = self._get_resource_metrics()

                    # Periodic heartbeat — DEBUG. At default 30s interval this is pure
                    # liveness noise unless someone is debugging the agent.
                    self.logger.debug(
                        "Agent heartbeat",
                        extra={
                            "heartbeat_n": self.metrics["heartbeats"],
                            "queue_size": current_queue_size,
                            "active_checks": active_checks,
                            "batch_size": len(self.reporter.current_batch),
                            "open_file_descriptors": (
                                resources.get("open_file_descriptors") if resources else None
                            ),
                            "fd_limit_soft": (
                                resources.get("fd_limit_soft") if resources else None
                            ),
                            "subprocess_count": (
                                resources.get("subprocess_count") if resources else None
                            ),
                        },
                    )

                last_queue_size = current_queue_size
                await asyncio.sleep(self.watchdog_interval)

            except Exception:
                self.logger.error("Error in watchdog monitor", exc_info=True)
                await asyncio.sleep(self.watchdog_interval)

        self.logger.info("Watchdog monitor stopped")

    def _get_resource_metrics(self) -> dict[str, Any]:
        """
        Get resource usage metrics (SWIRL-57: detect file descriptor leaks).

        Returns:
            Dictionary with resource metrics
        """
        if not self.resource_monitoring_enabled:
            return {}

        metrics: dict[str, Any] = {}

        try:
            # Get file descriptor count
            fd_count = self.process.num_fds()
            metrics["open_file_descriptors"] = fd_count

            # Get FD limit (soft limit)
            try:
                fd_soft_limit, fd_hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
                metrics["fd_limit_soft"] = fd_soft_limit
                metrics["fd_limit_hard"] = fd_hard_limit
                metrics["fd_usage_percent"] = round((fd_count / fd_soft_limit) * 100, 1)

                # Warn if approaching limit
                if metrics["fd_usage_percent"] >= self.resource_fd_warning_percent:
                    self.logger.warning(
                        "File descriptor usage HIGH - possible leak",
                        extra={
                            "fd_count": fd_count,
                            "fd_soft_limit": fd_soft_limit,
                            "fd_usage_percent": metrics["fd_usage_percent"],
                        },
                    )
            except Exception:
                self.logger.debug("Could not get FD limits", exc_info=True)

        except Exception:
            self.logger.debug("Could not get FD count", exc_info=True)

        try:
            # Get subprocess count (children of this process)
            children = self.process.children(recursive=True)
            metrics["subprocess_count"] = len(children)

            # Warn if too many subprocesses
            if metrics["subprocess_count"] >= self.resource_subprocess_warning_count:
                self.logger.warning(
                    "Subprocess count HIGH - possible leak",
                    extra={"subprocess_count": metrics["subprocess_count"]},
                )

        except Exception:
            self.logger.debug("Could not get subprocess count", exc_info=True)

        return metrics

    async def get_health(self) -> dict[str, Any]:
        """
        Get health information about the agent.

        Returns:
            Dictionary with health information
        """
        uptime = time.time() - self.metrics["start_time"]
        active_checks = len([t for t in asyncio.all_tasks() if "run_check" in str(t)])
        agent_id = self.config.get("agent_id", "unknown")

        health = {
            "agent_id": str(agent_id) if agent_id != "unknown" else "unknown",
            "uptime_seconds": uptime,
            "running": self.running,
            "checks_loaded": len(self.checks),
            "checks_executed": self.metrics["checks_executed"],
            "checks_succeeded": self.metrics["checks_succeeded"],
            "checks_failed": self.metrics["checks_failed"],
            "result_queue_size": self.result_queue.qsize(),
            "active_check_tasks": active_checks,
            "reporter_status": {
                "running": (self.reporter.running if hasattr(self.reporter, "running") else None),
                "batch_size": (
                    len(self.reporter.current_batch)
                    if hasattr(self.reporter, "current_batch")
                    else 0
                ),
                "batch_limit": (
                    self.reporter.batch_size if hasattr(self.reporter, "batch_size") else None
                ),
            },
        }

        # Add resource metrics (SWIRL-57)
        if self.resource_monitoring_enabled:
            health["resources"] = self._get_resource_metrics()

        return health

    def start(self) -> None:
        """Start the health monitor."""
        self.running = True

    def stop(self) -> None:
        """Stop the health monitor."""
        self.running = False
