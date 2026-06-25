"""
LuxSwirl Agent Core - Main agent implementation for the LuxSwirl monitoring system.

This module has been refactored to use a modular architecture with focused components:
- AgentRegistration: Handles registration and authentication
- CheckManager: Manages check configurations
- CheckExecutor: Executes individual checks
- ResultProcessor: Processes and reports results
- HeartbeatSender: Communicates with server
- HealthMonitor: Monitors agent health and watchdog
- LifecycleManager: Handles shutdown and signals
"""

import asyncio
import platform
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import psutil
from shared.logger import get_logger

from app.agent.check_executor import CheckExecutor
from app.agent.check_manager import CheckManager
from app.agent.credentials import AgentCredentials
from app.agent.health import HealthMonitor
from app.agent.heartbeat import HeartbeatSender
from app.agent.job_processor import JobProcessor
from app.agent.lifecycle import LifecycleManager
from app.agent.registration import AgentRegistration
from app.agent.reporter import Reporter
from app.agent.result_processor import ResultProcessor
from app.checks.base import BaseCheck


class LuxSwirlAgent:
    """Main agent class that orchestrates health check execution."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.logger = get_logger("luxswirl.agent")
        self.interval = config.get("interval", 60)
        self.checks: list[BaseCheck] = []

        # Load or register agent credentials
        self.credentials = AgentCredentials()
        if self.credentials.load():
            # Use loaded UUID
            self.config["agent_id"] = self.credentials.agent_id
            if self.credentials.api_key:
                self.config["auth_key"] = self.credentials.api_key
            self.logger.info(
                "Loaded agent credentials",
                extra={"agent_id": str(self.credentials.agent_id)},
            )
        else:
            self.logger.info("No credentials found - will register on startup")

        # Generate a unique run ID for this agent instance
        self.run_id = str(uuid.uuid4())

        # Concurrency control
        self.max_concurrent_checks = config.get("max_concurrent_checks", 200)
        self.semaphore = asyncio.Semaphore(self.max_concurrent_checks)

        # Queue for collecting results
        self.result_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Store the last state of checks
        self.last_state: dict[str, dict] = {}

        # Flag to control the main loop
        self.running = False

        # Cache file for offline capability
        storage_path = Path(config.get("report_storage_path", "reports"))
        storage_path.mkdir(parents=True, exist_ok=True)
        cache_file = storage_path / "checks_cache.json"

        # Internal metrics
        self.metrics = {
            "checks_executed": 0,
            "checks_succeeded": 0,
            "checks_failed": 0,
            "start_time": time.time(),
            "result_queue_max_size": 0,
            "heartbeats": 0,
        }

        # Get agent performance config
        self.result_queue_timeout: float = config.get("result_queue_timeout", 1.0)
        self.result_processor_retry_delay: float = config.get("result_processor_retry_delay", 1.0)
        self.shutdown_timeout: float = config.get("shutdown_timeout", 5.0)
        self.main_loop_sleep: float = config.get("main_loop_sleep", 0.1)
        self.heartbeat_interval: int = config.get("heartbeat_interval", 60)
        self.watchdog_enabled: bool = config.get("watchdog_enabled", True)
        self.watchdog_interval: int = config.get("watchdog_interval", 30)
        self.watchdog_stall_threshold: int = config.get("watchdog_stall_threshold", 3)

        # Create reporter
        self.reporter = Reporter(config)

        # Create job processor
        self.job_processor = JobProcessor(config)

        # Task references
        self.result_processor_task: asyncio.Task[None] | None = None
        self.self_monitor_task: asyncio.Task[None] | None = None
        self.watchdog_task: asyncio.Task[None] | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None

        # Collect system info once
        self.hostname: str = platform.node()
        self.ip_address: str | None = None  # Will be populated if available

        # Get process handle for metrics
        self.process = psutil.Process()
        # Initialize CPU baseline (first call returns 0.0, subsequent calls are accurate)
        self.process.cpu_percent(interval=None)

        # Initialize modular components
        self._initialize_modules(cache_file)

    def _initialize_modules(self, cache_file: Path) -> None:
        """Initialize all agent module components."""
        # Registration module
        self.registration = AgentRegistration(
            config=self.config,
            credentials=self.credentials,
        )
        self.registration.hostname = self.hostname

        # Check manager
        self.check_manager = CheckManager(
            config=self.config,
            cache_file=cache_file,
            credentials=self.credentials,
            on_orphaned_agent=self._handle_orphaned_agent,
        )

        # Check executor
        self.check_executor = CheckExecutor(
            config=self.config,
            metrics=self.metrics,
            check_stats=self.check_manager.check_stats,
            semaphore=self.semaphore,
            result_queue=self.result_queue,
            last_state=self.last_state,
            run_id=self.run_id,
        )

        # Result processor
        self.result_processor = ResultProcessor(
            config=self.config,
            reporter=self.reporter,
            result_queue=self.result_queue,
            result_queue_timeout=self.result_queue_timeout,
            result_processor_retry_delay=self.result_processor_retry_delay,
        )

        # Heartbeat sender
        self.heartbeat_sender = HeartbeatSender(
            config=self.config,
            metrics=self.metrics,
            reporter=self.reporter,
            job_processor=self.job_processor,
            credentials=self.credentials,
            process=self.process,
            on_config_reload=self._reload_checks,
            on_agent_approved=self._handle_agent_approved,
        )
        self.heartbeat_sender.hostname = self.hostname
        self.heartbeat_sender.ip_address = self.ip_address
        self.heartbeat_sender.checks = self.checks
        self.heartbeat_sender.result_queue = self.result_queue
        self.heartbeat_sender.semaphore = self.semaphore

        # Health monitor
        self.health_monitor = HealthMonitor(
            config=self.config,
            metrics=self.metrics,
            reporter=self.reporter,
            result_queue=self.result_queue,
            checks=self.checks,
            watchdog_interval=self.watchdog_interval,
            watchdog_stall_threshold=self.watchdog_stall_threshold,
            heartbeat_interval=self.heartbeat_interval,
        )

        # Lifecycle manager
        self.lifecycle_manager = LifecycleManager(
            config=self.config,
            metrics=self.metrics,
            result_queue=self.result_queue,
            reporter=self.reporter,
            job_processor=self.job_processor,
            heartbeat_sender=self.heartbeat_sender,
            run_id=self.run_id,
            shutdown_timeout=self.shutdown_timeout,
        )

    # ========================================================================
    # Callback methods for module coordination
    # ========================================================================

    async def _handle_orphaned_agent(self) -> bool:
        """Handle orphaned agent re-registration (callback for check_manager)."""
        return await self.registration.register_with_server()

    async def _reload_checks(self) -> None:
        """Reload checks from server (callback for heartbeat_sender)."""
        self.checks = await self.check_manager.load_checks()
        # Update references in other modules
        self.heartbeat_sender.checks = self.checks
        self.health_monitor.checks = self.checks

    async def _handle_agent_approved(self) -> bool:
        """Handle agent approval and key recovery (callback for heartbeat_sender)."""
        success = await self.registration.recover_agent_key()
        if success:
            new_auth_key = self.config["auth_key"]

            # Reporter caches auth_key and bakes the Authorization header into
            # its httpx client at start(). Refresh both so subsequent reports
            # use the new agent-specific key instead of the registration token.
            if self.reporter:
                self.reporter.auth_key = new_auth_key
                if self.reporter.client:
                    await self.reporter.client.aclose()
                    self.reporter.client = httpx.AsyncClient(
                        timeout=30.0,
                        headers={
                            "User-Agent": "LuxSwirl-Agent/1.0",
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {new_auth_key}",
                        },
                    )
                    self.logger.info("Updated reporter client with agent-specific key")

            # Update job processor client with new key
            if self.job_processor and self.job_processor.client:
                await self.job_processor.client.aclose()
                job_timeout = self.config.get("job_timeout", 30.0)
                self.job_processor.client = httpx.AsyncClient(
                    timeout=job_timeout,
                    headers={
                        "User-Agent": "LuxSwirl-Agent/1.0",
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {new_auth_key}",
                    },
                )
                self.logger.info("Updated job processor client with agent-specific key")
        return success

    # ========================================================================
    # Public API methods (delegate to modules)
    # ========================================================================

    def register_check_type(self, name: str, check_class: type[BaseCheck]) -> None:
        """
        Register a check type with the agent.

        Args:
            name: The name of the check type
            check_class: The class implementing the check
        """
        self.check_manager.register_check_type(name, check_class)

    def register_job_type(self, job_class) -> None:
        """
        Register a job handler type with the agent.

        Args:
            job_class: The class implementing the job handler
        """
        self.job_processor.register_job_type(job_class)
        self.logger.info(
            "Registered job type",
            extra={"job_type": job_class.job_type},
        )

    async def load_checks(self) -> None:
        """Load checks from server API (or fallback to cache/local config)."""
        self.checks = await self.check_manager.load_checks()
        # Update references in other modules
        self.heartbeat_sender.checks = self.checks
        self.health_monitor.checks = self.checks

    async def run_check(self, check: BaseCheck) -> None:
        """
        Run a single check and collect the result.

        Args:
            check: The check to run
        """
        await self.check_executor.run_check(check)

    async def get_health(self) -> dict[str, Any]:
        """
        Get health information about the agent.

        Returns:
            Dictionary with health information
        """
        return await self.health_monitor.get_health()

    # ========================================================================
    # Main lifecycle methods
    # ========================================================================

    def register_signal_handlers(self) -> None:
        """Register signal handlers for graceful shutdown."""
        self.lifecycle_manager.register_signal_handlers()

    async def shutdown(self, signal=None) -> None:
        """
        Shutdown the agent gracefully.

        Args:
            signal: Optional signal that triggered shutdown
        """
        # Update task references for lifecycle manager
        self.lifecycle_manager.result_processor_task = self.result_processor_task
        self.lifecycle_manager.self_monitor_task = self.self_monitor_task
        self.lifecycle_manager.watchdog_task = self.watchdog_task
        self.lifecycle_manager.heartbeat_task = self.heartbeat_task
        self.lifecycle_manager.running = self.running

        await self.lifecycle_manager.shutdown(signal)

        # Sync running state back
        self.running = self.lifecycle_manager.running

    async def run(self) -> None:
        """Main run loop for the agent."""
        self.logger.info("Starting LuxSwirl Agent")

        # Register with server if we don't have credentials
        if not self.credentials.has_credentials():
            self.logger.info("No credentials found - registering with server")
            if await self.registration.register_with_server():
                self.logger.info(
                    "Registration successful",
                    extra={"agent_id": str(self.config.get("agent_id"))},
                )
            else:
                self.logger.error("Failed to register with server - cannot continue")
                return

        self.logger.info(
            "Agent ID",
            extra={"agent_id": str(self.config.get("agent_id"))},
        )

        # Register signal handlers
        self.register_signal_handlers()

        # Load checks
        await self.load_checks()

        if not self.checks:
            self.logger.warning("No checks loaded - agent will run in job-only mode")
        else:
            self.logger.info(
                "Loaded checks",
                extra={"check_count": len(self.checks)},
            )

        # Initialize check run times
        last_run = {check.name: 0.0 for check in self.checks}

        # Start the background tasks
        self.running = True

        # Start all module components
        self.result_processor.running = True
        self.heartbeat_sender.running = True
        self.health_monitor.running = True
        self.lifecycle_manager.running = True

        # Start result processor
        self.result_processor_task = asyncio.create_task(self.result_processor.process_results())

        # Start watchdog task if enabled
        if self.watchdog_enabled:
            self.logger.info("Enabling watchdog monitor")
            self.watchdog_task = asyncio.create_task(
                self.health_monitor.monitor_result_processing()
            )

        # Start heartbeat sender
        if self.config.get("enable_heartbeat", True):
            self.logger.info("Enabling heartbeat sender")
            self.heartbeat_task = asyncio.create_task(self.heartbeat_sender.send_heartbeat())

        # Start job processor
        await self.job_processor.start()
        self.logger.info("Job processor started")

        try:
            # Log initial heartbeat
            self.logger.info(
                "Agent starting",
                extra={
                    "check_count": len(self.checks),
                    "max_concurrent_checks": self.max_concurrent_checks,
                    "batch_size": self.reporter.batch_size,
                },
            )

            # Main check loop
            while self.running:
                now = time.time()

                # Check if any checks need to be run
                for check in self.checks:
                    interval = check.config.get("interval", self.interval)
                    # Initialize last_run for new checks
                    if check.name not in last_run:
                        last_run[check.name] = 0.0
                    if now - last_run[check.name] >= interval:
                        last_run[check.name] = now
                        asyncio.create_task(self.run_check(check))

                # Sleep a short time to avoid high CPU usage and allow for clean shutdown
                for _ in range(int(0.5 / self.main_loop_sleep)):  # ~0.5 seconds total
                    if not self.running:
                        break
                    await asyncio.sleep(self.main_loop_sleep)

        except asyncio.CancelledError:
            self.logger.info("Agent task cancelled")
            await self.shutdown()
        except Exception:
            self.logger.error("Error in main loop", exc_info=True)
            await self.shutdown()
            raise
        finally:
            # Ensure clean shutdown
            if self.running:
                await self.shutdown()
