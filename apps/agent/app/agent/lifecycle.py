"""
Agent lifecycle management.

Handles agent startup, shutdown, and signal handling including:
- Graceful shutdown coordination
- Final metrics reporting
- Task cleanup
- Signal handler registration
"""

import asyncio
import signal
from collections.abc import Callable

from shared.logger import get_logger

logger = get_logger("luxswirl.agent.lifecycle")


class LifecycleManager:
    """Manages agent lifecycle including shutdown and signal handling."""

    def __init__(
        self,
        config: dict,
        metrics: dict,
        result_queue: asyncio.Queue,
        reporter,
        job_processor,
        heartbeat_sender,
        run_id: str,
        shutdown_timeout: float = 5.0,
    ):
        """
        Initialize lifecycle manager.

        Args:
            config: Agent configuration
            metrics: Agent metrics dict
            result_queue: Result queue for final processing
            reporter: Reporter instance
            job_processor: JobProcessor instance
            heartbeat_sender: HeartbeatSender instance for shutdown heartbeat
            run_id: Unique run ID for this agent instance
            shutdown_timeout: Max seconds to wait for tasks during shutdown
        """
        self.config = config
        self.metrics = metrics
        self.result_queue = result_queue
        self.reporter = reporter
        self.job_processor = job_processor
        self.heartbeat_sender = heartbeat_sender
        self.run_id = run_id
        self.shutdown_timeout = shutdown_timeout
        self.logger = logger

        # Control flag (set by parent)
        self.running = False

        # Task references (set by parent)
        self.result_processor_task: asyncio.Task[None] | None = None
        self.self_monitor_task: asyncio.Task[None] | None = None
        self.watchdog_task: asyncio.Task[None] | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None

    def register_signal_handlers(self) -> None:
        """Register signal handlers for graceful shutdown."""
        signals = (signal.SIGTERM, signal.SIGINT)
        for s in signals:

            def make_handler(sig: signal.Signals) -> Callable[[], None]:
                def handler() -> None:
                    asyncio.create_task(self.shutdown(sig))

                return handler

            asyncio.get_event_loop().add_signal_handler(s, make_handler(s))

    async def shutdown(self, signal=None) -> None:
        """
        Shutdown the agent gracefully.

        Args:
            signal: Optional signal that triggered shutdown
        """
        if signal:
            self.logger.info(
                "Received exit signal",
                extra={"signal_name": signal.name},
            )

        if not self.running:
            # Already shutting down
            return

        self.logger.info("Shutting down...")
        self.running = False

        try:
            # Send final shutdown heartbeat to server
            await self.heartbeat_sender.send_shutdown_heartbeat(self.run_id)

            # Wait for tasks to complete with timeout
            wait_tasks: list[asyncio.Task[None]] = []

            if self.result_processor_task and not self.result_processor_task.done():
                wait_tasks.append(self.result_processor_task)

            if self.self_monitor_task and not self.self_monitor_task.done():
                wait_tasks.append(self.self_monitor_task)

            if self.watchdog_task and not self.watchdog_task.done():
                wait_tasks.append(self.watchdog_task)

            if self.heartbeat_task and not self.heartbeat_task.done():
                wait_tasks.append(self.heartbeat_task)

            if wait_tasks:
                try:
                    # Wait for up to shutdown_timeout seconds for tasks to complete
                    await asyncio.wait_for(
                        asyncio.gather(*wait_tasks, return_exceptions=True),
                        timeout=self.shutdown_timeout,
                    )
                except TimeoutError:
                    self.logger.warning(
                        "Timed out waiting for tasks to complete",
                        extra={"shutdown_timeout_seconds": self.shutdown_timeout},
                    )

            # Process any remaining results
            while not self.result_queue.empty():
                try:
                    results = []
                    # Get up to 50 results
                    for _ in range(min(50, self.result_queue.qsize())):
                        if not self.result_queue.empty():
                            results.append(await self.result_queue.get())

                    if results:
                        self.logger.info(
                            "Reporting final batch",
                            extra={"result_count": len(results)},
                        )
                        for result in results:
                            await self.reporter.add_result(result)
                except Exception:
                    self.logger.error("Error processing final results", exc_info=True)
                    break

            # Stop the reporter (after processing final results)
            await self.reporter.stop()

            # Stop the job processor
            await self.job_processor.stop()

        except Exception:
            self.logger.error("Error during shutdown", exc_info=True)

        self.logger.info("Shutdown complete")

    def start(self) -> None:
        """Start lifecycle management."""
        self.running = True

    def stop(self) -> None:
        """Stop lifecycle management (triggers shutdown)."""
        self.running = False
