"""
Agent heartbeat management.

Handles periodic heartbeat communication with the server, including:
- System metrics collection (CPU, memory, queue stats)
- Dynamic configuration updates from server
- Agent approval status monitoring
- Job assignment reception
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from shared.logger import get_logger

if TYPE_CHECKING:
    from app.checks.base import BaseCheck

logger = get_logger("luxswirl.agent.heartbeat")


class HeartbeatSender:
    """Manages periodic heartbeat communication with the server."""

    def __init__(
        self,
        config: dict,
        metrics: dict,
        reporter,
        job_processor,
        credentials,
        process,
        # Callbacks to agent methods
        on_config_reload,
        on_agent_approved,
    ):
        """
        Initialize heartbeat sender.

        Args:
            config: Agent configuration
            metrics: Agent metrics dict
            reporter: Reporter instance for batch metrics
            job_processor: JobProcessor instance for job stats
            credentials: AgentCredentials instance
            process: psutil.Process for system metrics
            on_config_reload: Callback to reload checks when config changes
            on_agent_approved: Callback when agent gets approved (to update HTTP clients)
        """
        self.config = config
        self.metrics = metrics
        self.reporter = reporter
        self.job_processor = job_processor
        self.credentials = credentials
        self.process = process
        self.on_config_reload = on_config_reload
        self.on_agent_approved = on_agent_approved
        self.logger = logger

        # State
        self.running = False
        self.heartbeat_client: httpx.AsyncClient | None = None
        self.last_heartbeat_time = time.time()

        # Dynamic configuration (can be updated via heartbeat response)
        self.heartbeat_interval = config.get("heartbeat_interval", 60)
        self.max_concurrent_checks = config.get("max_concurrent_checks", 200)
        self.watchdog_interval = config.get("watchdog_interval", 30)
        self.watchdog_stall_threshold = config.get("watchdog_stall_threshold", 3)

        # Config version tracking
        self.config_version = None

        # Agent state (updated by parent)
        self.hostname: str | None = None
        self.ip_address: str | None = None
        self.checks: list[BaseCheck] = []
        self.result_queue: asyncio.Queue[dict[str, Any]] | None = None
        self.semaphore: asyncio.Semaphore | None = None

    async def send_heartbeat(self) -> None:
        """Send periodic heartbeat to server with agent health metrics."""
        self.logger.info("Starting heartbeat sender")

        # Get server URL from config
        push_url = self.config.get("push_url", "http://localhost:9000")
        if not push_url:
            self.logger.warning("No push_url configured, heartbeat disabled")
            return

        agent_id = self.config.get("agent_id")
        if not agent_id:
            self.logger.warning("No agent_id configured, heartbeat disabled")
            return

        # Extract base URL
        if "/api/v1" in push_url:
            base_url = push_url.split("/api/v1")[0]
        else:
            base_url = push_url.rstrip("/")

        heartbeat_url = f"{base_url}/api/v1/heartbeat"
        auth_key = self.config.get("auth_key")

        # Create HTTP client
        headers = {}
        if auth_key:
            headers["Authorization"] = f"Bearer {auth_key}"

        self.heartbeat_client = httpx.AsyncClient(
            headers=headers,
            timeout=10.0,
        )

        try:
            while self.running:
                try:
                    # Calculate uptime
                    uptime_seconds = int(time.time() - self.metrics["start_time"])

                    # Get CPU, memory, and reporter metrics (run in thread pool to avoid blocking)
                    try:
                        loop = asyncio.get_event_loop()

                        def get_system_metrics():
                            # Use 5-second interval for accurate CPU measurement
                            # (heartbeat runs every 60s, so this is non-blocking)
                            cpu = self.process.cpu_percent(interval=5.0)
                            mem = self.process.memory_info()
                            return cpu, int(mem.rss / 1024 / 1024)

                        # Run both system metrics and reporter metrics in thread pool
                        cpu_percent, memory_mb = await loop.run_in_executor(
                            None, get_system_metrics
                        )
                        reporter_metrics = (
                            await loop.run_in_executor(None, self.reporter.get_metrics)
                            if hasattr(self.reporter, "get_metrics")
                            else {}
                        )
                    except Exception:
                        self.logger.warning("Failed to get system metrics", exc_info=True)
                        cpu_percent = None
                        memory_mb = None
                        reporter_metrics = {}

                    # Get job queue stats
                    job_stats = self.job_processor.get_stats()

                    # Get resource monitoring metrics (SWIRL-57)
                    resource_metrics = {}
                    if self.config.get("resource_monitoring_enabled", True):
                        try:
                            import resource as sys_resource

                            import psutil

                            proc = psutil.Process()
                            fd_count = proc.num_fds()
                            fd_soft_limit, _ = sys_resource.getrlimit(sys_resource.RLIMIT_NOFILE)
                            children = proc.children(recursive=True)
                            resource_metrics = {
                                "open_file_descriptors": fd_count,
                                "fd_limit_soft": fd_soft_limit,
                                "fd_usage_percent": round((fd_count / fd_soft_limit) * 100, 1),
                                "subprocess_count": len(children),
                            }
                        except Exception:
                            self.logger.debug("Could not get resource metrics", exc_info=True)

                    # Build heartbeat payload
                    heartbeat = {
                        "agent_id": str(agent_id),
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "hostname": self.hostname,
                        "ip_address": self.ip_address,
                        "version": os.getenv("APP_VERSION", "dev"),
                        "uptime_seconds": uptime_seconds,
                        "status": "online" if self.running else "offline",
                        "tags": self.config.get("tags", []),
                        # Check stats
                        "checks_total": len(self.checks),
                        "checks_active": len(
                            [t for t in asyncio.all_tasks() if "run_check" in str(t)]
                        ),
                        "checks_executed_count": self.metrics["checks_executed"],
                        "checks_succeeded_count": self.metrics["checks_succeeded"],
                        "checks_failed_count": self.metrics["checks_failed"],
                        # Performance metrics
                        "cpu_percent": cpu_percent,
                        "memory_mb": memory_mb,
                        "queue_depth": (self.result_queue.qsize() if self.result_queue else 0),
                        "queue_max_size": self.metrics["result_queue_max_size"],
                        # Config state
                        "config_version": self.config_version,
                        "baseline_checks_count": len(self.checks),
                        "remote_checks_count": 0,
                        # Job queue stats
                        "jobs_pending": job_stats.get("jobs_pending", 0),
                        "jobs_running": job_stats.get("jobs_running", 0),
                        "jobs_completed_since_last": job_stats.get("jobs_completed_since_last", 0),
                        "jobs_failed_since_last": job_stats.get("jobs_failed_since_last", 0),
                        # Error tracking
                        "errors_since_last_heartbeat": 0,
                        "warnings_since_last_heartbeat": 0,
                        "last_error_message": None,
                        "server_unreachable_count": reporter_metrics.get("failed_batches", 0),
                        # Reporter backlog metrics
                        "stored_reports_count": reporter_metrics.get("stored_reports_count", 0),
                        "stored_reports_oldest_timestamp": reporter_metrics.get(
                            "stored_reports_oldest_timestamp"
                        ),
                        # Resource monitoring (SWIRL-57)
                        "open_file_descriptors": resource_metrics.get("open_file_descriptors"),
                        "fd_limit_soft": resource_metrics.get("fd_limit_soft"),
                        "fd_usage_percent": resource_metrics.get("fd_usage_percent"),
                        "subprocess_count": resource_metrics.get("subprocess_count"),
                    }

                    # Send heartbeat
                    response = await self.heartbeat_client.post(heartbeat_url, json=heartbeat)

                    if response.status_code == 200:
                        self.metrics["heartbeats"] += 1
                        self.last_heartbeat_time = time.time()

                        # Parse heartbeat response
                        try:
                            response_data = response.json()

                            # Update intervals if provided
                            new_heartbeat_interval = response_data.get(
                                "heartbeat_interval", self.heartbeat_interval
                            )
                            if new_heartbeat_interval != self.heartbeat_interval:
                                self.logger.info(
                                    "Updating heartbeat_interval",
                                    extra={
                                        "from_seconds": self.heartbeat_interval,
                                        "to_seconds": new_heartbeat_interval,
                                    },
                                )
                                self.heartbeat_interval = new_heartbeat_interval

                            # Update reporter configuration if provided
                            self._update_reporter_config(response_data)

                            # Update performance tuning configuration if provided
                            self._update_performance_config(response_data)

                            # Update logging configuration if provided
                            self._update_logging_config(response_data)

                            # Check if config version changed and reload if needed
                            await self._handle_config_version(response_data)

                            # Check if agent has been approved and needs to retrieve agent-specific key
                            await self._handle_agent_approval(response_data)

                            # Process incoming jobs
                            jobs = response_data.get("jobs", [])
                            if jobs:
                                self.logger.info(
                                    "Received jobs from server",
                                    extra={"job_count": len(jobs)},
                                )
                                await self.job_processor.enqueue_jobs(jobs)

                            self.logger.debug(
                                "Heartbeat sent successfully",
                                extra={
                                    "uptime_seconds": uptime_seconds,
                                    "config_version": self.config_version,
                                    "job_count": len(jobs),
                                },
                            )
                        except Exception:
                            self.logger.warning(
                                "Failed to parse heartbeat response",
                                exc_info=True,
                            )
                    else:
                        self.logger.warning(
                            "Heartbeat failed",
                            extra={
                                "status_code": response.status_code,
                                "response_text": response.text,
                            },
                        )

                except httpx.RequestError:
                    self.logger.warning("Failed to send heartbeat", exc_info=True)
                except Exception:
                    self.logger.error("Error sending heartbeat", exc_info=True)

                # Check if we should continue
                if not self.running:
                    break

                # Wait for heartbeat_interval with periodic checks for shutdown
                for _ in range(self.heartbeat_interval):
                    if not self.running:
                        break
                    await asyncio.sleep(1)

        except asyncio.CancelledError:
            pass
        except Exception:
            self.logger.error("Unhandled exception in heartbeat sender", exc_info=True)
        finally:
            if self.heartbeat_client:
                await self.heartbeat_client.aclose()

        self.logger.info("Heartbeat sender stopped")

    def _update_reporter_config(self, response_data: dict) -> None:
        """Update reporter configuration from heartbeat response."""
        reporter_updates = {}

        if "report_interval" in response_data and response_data["report_interval"] is not None:
            new_val = response_data["report_interval"]
            if new_val != self.reporter.report_interval:
                self.logger.info(
                    "Updating report_interval",
                    extra={"from_seconds": self.reporter.report_interval, "to_seconds": new_val},
                )
                self.reporter.report_interval = new_val
                reporter_updates["report_interval"] = new_val

        if "report_batch_size" in response_data and response_data["report_batch_size"] is not None:
            new_val = response_data["report_batch_size"]
            if new_val != self.reporter.batch_size:
                self.logger.info(
                    "Updating report_batch_size",
                    extra={"from_value": self.reporter.batch_size, "to_value": new_val},
                )
                self.reporter.batch_size = new_val
                reporter_updates["report_batch_size"] = new_val

        if (
            "report_max_files_per_batch" in response_data
            and response_data["report_max_files_per_batch"] is not None
        ):
            new_val = response_data["report_max_files_per_batch"]
            if new_val != self.reporter.max_files_per_batch:
                self.logger.info(
                    "Updating report_max_files_per_batch",
                    extra={"from_value": self.reporter.max_files_per_batch, "to_value": new_val},
                )
                self.reporter.max_files_per_batch = new_val
                reporter_updates["report_max_files_per_batch"] = new_val

        if (
            "report_process_interval" in response_data
            and response_data["report_process_interval"] is not None
        ):
            new_val = response_data["report_process_interval"]
            if new_val != self.reporter.process_interval:
                self.logger.info(
                    "Updating report_process_interval",
                    extra={"from_seconds": self.reporter.process_interval, "to_seconds": new_val},
                )
                self.reporter.process_interval = new_val
                reporter_updates["report_process_interval"] = new_val

        if (
            "report_max_queue_size" in response_data
            and response_data["report_max_queue_size"] is not None
        ):
            new_val = response_data["report_max_queue_size"]
            if new_val != self.reporter.max_queue_size:
                self.logger.info(
                    "Updating report_max_queue_size",
                    extra={"from_value": self.reporter.max_queue_size, "to_value": new_val},
                )
                self.reporter.max_queue_size = new_val
                reporter_updates["report_max_queue_size"] = new_val

        if (
            "report_backpressure_threshold" in response_data
            and response_data["report_backpressure_threshold"] is not None
        ):
            new_val = response_data["report_backpressure_threshold"]
            if new_val != self.reporter.backpressure_threshold:
                self.logger.info(
                    "Updating report_backpressure_threshold",
                    extra={
                        "from_value": self.reporter.backpressure_threshold,
                        "to_value": new_val,
                    },
                )
                self.reporter.backpressure_threshold = new_val
                reporter_updates["report_backpressure_threshold"] = new_val

    def _update_performance_config(self, response_data: dict) -> None:
        """Update performance tuning configuration from heartbeat response."""
        if (
            "max_concurrent_checks" in response_data
            and response_data["max_concurrent_checks"] is not None
        ):
            new_val = response_data["max_concurrent_checks"]
            if new_val != self.max_concurrent_checks:
                self.logger.info(
                    "Updating max_concurrent_checks",
                    extra={"from_value": self.max_concurrent_checks, "to_value": new_val},
                )
                # Update the semaphore by recreating it
                if self.semaphore:
                    self.semaphore = asyncio.Semaphore(new_val)
                self.max_concurrent_checks = new_val

        if "watchdog_interval" in response_data and response_data["watchdog_interval"] is not None:
            new_val = response_data["watchdog_interval"]
            if new_val != self.watchdog_interval:
                self.logger.info(
                    "Updating watchdog_interval",
                    extra={"from_seconds": self.watchdog_interval, "to_seconds": new_val},
                )
                self.watchdog_interval = new_val

        if (
            "watchdog_stall_threshold" in response_data
            and response_data["watchdog_stall_threshold"] is not None
        ):
            new_val = response_data["watchdog_stall_threshold"]
            if new_val != self.watchdog_stall_threshold:
                self.logger.info(
                    "Updating watchdog_stall_threshold",
                    extra={"from_value": self.watchdog_stall_threshold, "to_value": new_val},
                )
                self.watchdog_stall_threshold = new_val

    def _update_logging_config(self, response_data: dict) -> None:
        """Update logging configuration from heartbeat response."""
        if "log_level" in response_data and response_data["log_level"] is not None:
            new_level = response_data["log_level"].upper()
            current_level = logging.getLevelName(self.logger.level)
            if new_level != current_level:
                self.logger.info(
                    "Updating log_level",
                    extra={"from_level": current_level, "to_level": new_level},
                )
                # Update logger level
                numeric_level = getattr(logging, new_level, None)
                if isinstance(numeric_level, int):
                    self.logger.setLevel(numeric_level)
                    # Also update root logger
                    logging.getLogger().setLevel(numeric_level)
                else:
                    self.logger.warning(
                        "Invalid log level",
                        extra={"new_level": new_level},
                    )

    async def _handle_config_version(self, response_data: dict) -> None:
        """Handle config version changes and trigger reload if needed."""
        config_version = response_data.get("config_version")
        if config_version:
            if self.config_version is None:
                # First time getting config version, just store it
                self.config_version = config_version
                self.logger.debug(
                    "Initial config version set",
                    extra={"config_version": config_version},
                )
            elif config_version != self.config_version:
                # Version changed, reload needed
                self.logger.info(
                    "Config version changed - reloading checks",
                    extra={
                        "from_version": self.config_version,
                        "to_version": config_version,
                    },
                )
                # Store the new version before reloading so load_checks saves with correct version
                self.config_version = config_version
                # Call the callback to reload checks
                if self.on_config_reload:
                    await self.on_config_reload()
                    self.logger.info(
                        "Reloaded checks from server",
                        extra={"check_count": len(self.checks)},
                    )
            else:
                # Same version, no reload needed
                self.logger.debug(
                    "Config version unchanged",
                    extra={"config_version": config_version},
                )

    async def _handle_agent_approval(self, response_data: dict) -> None:
        """Handle agent approval status and retrieve agent-specific key if needed."""
        approval_status = response_data.get("approval_status")
        if approval_status == "active" and not self.credentials.api_key:
            self.logger.info("Agent approved! Retrieving agent-specific API key...")
            # Call the callback to handle approval (will update HTTP clients)
            if self.on_agent_approved:
                success = await self.on_agent_approved()
                if success:
                    self.logger.info("Agent-specific key retrieved successfully")
                    # Update heartbeat client headers with new key
                    if self.heartbeat_client:
                        await self.heartbeat_client.aclose()
                        self.heartbeat_client = httpx.AsyncClient(
                            headers={"Authorization": f"Bearer {self.config['auth_key']}"},
                            timeout=10.0,
                        )
                else:
                    self.logger.warning(
                        "Failed to retrieve agent-specific key, will retry on next heartbeat"
                    )

    def start(self) -> None:
        """Start the heartbeat sender."""
        self.running = True

    def stop(self) -> None:
        """Stop the heartbeat sender."""
        self.running = False

    async def send_shutdown_heartbeat(self, run_id: str) -> None:
        """
        Send a final shutdown heartbeat to the server.

        Args:
            run_id: Unique run ID for this agent instance
        """
        agent_id = self.config.get("agent_id")
        if not agent_id:
            self.logger.warning("No agent_id configured, cannot send shutdown heartbeat")
            return

        push_url = self.config.get("push_url", "http://localhost:9000")
        if not push_url:
            self.logger.warning("No push_url configured, cannot send shutdown heartbeat")
            return

        # Extract base URL
        if "/api/v1" in push_url:
            base_url = push_url.split("/api/v1")[0]
        else:
            base_url = push_url.rstrip("/")

        heartbeat_url = f"{base_url}/api/v1/heartbeat"
        auth_key = self.config.get("auth_key")

        # Create temporary HTTP client
        headers = {}
        if auth_key:
            headers["Authorization"] = f"Bearer {auth_key}"

        client = httpx.AsyncClient(
            headers=headers,
            timeout=10.0,
        )

        try:
            # Calculate final uptime
            uptime_seconds = int(time.time() - self.metrics["start_time"])

            # Build shutdown heartbeat payload
            heartbeat = {
                "agent_id": str(agent_id),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "hostname": self.hostname,
                "ip_address": self.ip_address,
                "version": os.getenv("APP_VERSION", "dev"),
                "uptime_seconds": uptime_seconds,
                "status": "offline",
                "tags": self.config.get("tags", []),
                # Check stats
                "checks_total": len(self.checks),
                "checks_active": 0,  # Shutdown - no checks running
                "checks_executed_count": self.metrics["checks_executed"],
                "checks_succeeded_count": self.metrics["checks_succeeded"],
                "checks_failed_count": self.metrics["checks_failed"],
                # Performance metrics
                "cpu_percent": None,
                "memory_mb": None,
                "queue_depth": self.result_queue.qsize() if self.result_queue else 0,
                "queue_max_size": self.metrics["result_queue_max_size"],
                # Config state
                "config_version": self.config_version,
                "baseline_checks_count": len(self.checks),
                "remote_checks_count": 0,
                # Job queue stats
                "jobs_pending": 0,
                "jobs_running": 0,
                "jobs_completed_since_last": 0,
                "jobs_failed_since_last": 0,
                # Error tracking
                "errors_since_last_heartbeat": 0,
                "warnings_since_last_heartbeat": 0,
                "last_error_message": None,
                "server_unreachable_count": 0,
                # Reporter backlog metrics
                "stored_reports_count": 0,
                "stored_reports_oldest_timestamp": None,
                # Resource monitoring
                "open_file_descriptors": None,
                "fd_limit_soft": None,
                "fd_usage_percent": None,
                "subprocess_count": None,
                # Shutdown tracking (NEW)
                "is_shutdown": True,
                "agent_run_id": run_id,
                "heartbeats_total": self.metrics["heartbeats"],
            }

            # Send shutdown heartbeat
            self.logger.info(
                "Sending shutdown heartbeat",
                extra={
                    "uptime_seconds": uptime_seconds,
                    "heartbeats_total": self.metrics["heartbeats"],
                },
            )
            response = await client.post(heartbeat_url, json=heartbeat)

            if response.status_code == 200:
                self.logger.info("Shutdown heartbeat sent successfully")
            else:
                self.logger.warning(
                    "Shutdown heartbeat failed",
                    extra={
                        "status_code": response.status_code,
                        "response_text": response.text,
                    },
                )
        except httpx.RequestError:
            self.logger.warning("Failed to send shutdown heartbeat", exc_info=True)
        except Exception:
            self.logger.error("Error sending shutdown heartbeat", exc_info=True)
        finally:
            await client.aclose()
