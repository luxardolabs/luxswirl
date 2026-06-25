"""
Job processor for agent-side job execution.

Manages a priority queue of jobs, enforces concurrency limits,
and handles job execution with proper error handling.
"""

import asyncio
import time
from typing import Any

import httpx
from shared.jobs.base import BaseJob
from shared.logger import get_logger


class JobProcessor:
    """
    Processes jobs received from the server via heartbeat.

    Features:
    - Priority queue (higher priority runs first)
    - Concurrency control (configurable max concurrent jobs)
    - Job type registry (pluggable job handlers)
    - Result submission to server
    - Queue statistics for heartbeat reporting
    """

    def __init__(self, config: dict[str, Any]):
        """
        Initialize job processor.

        Args:
            config: Agent configuration dictionary
        """
        self.config = config
        self.logger = get_logger("luxswirl.agent.job_processor")

        # Concurrency control
        self.max_concurrent = config.get("job_queue_size", 5)
        self.semaphore = asyncio.Semaphore(self.max_concurrent)

        # Job queue (priority, timestamp, job_data)
        self.job_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()

        # Active jobs tracking
        self.active_jobs: dict[str, asyncio.Task] = {}

        # Job type registry
        self.job_registry: dict[str, type[BaseJob]] = {}

        # Statistics (reset on each heartbeat)
        self.stats = {
            "jobs_completed_since_last": 0,
            "jobs_failed_since_last": 0,
            "total_completed": 0,
            "total_failed": 0,
        }

        # HTTP client for result submission
        self.client: httpx.AsyncClient | None = None

        # Running flag
        self.running = False

        # Background worker task
        self.worker_task: asyncio.Task | None = None

    def register_job_type(self, job_class: type[BaseJob]) -> None:
        """
        Register a job handler class.

        Args:
            job_class: Job handler class (must inherit from BaseJob)
        """
        job_type = job_class.job_type
        self.job_registry[job_type] = job_class
        self.logger.info(
            "Registered job type",
            extra={"job_type": job_type},
        )

    async def start(self) -> None:
        """Start the job processor."""
        if self.running:
            return

        self.running = True

        # Create HTTP client
        auth_key = self.config.get("auth_key")
        job_timeout = self.config.get("job_timeout", 30.0)
        self.client = httpx.AsyncClient(
            timeout=job_timeout,
            headers={
                "User-Agent": "LuxSwirl-Agent/1.0",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {auth_key}" if auth_key else "",
            },
        )

        # Start background worker
        self.worker_task = asyncio.create_task(self._job_worker())

        self.logger.info(
            "Job processor started",
            extra={"max_concurrent": self.max_concurrent},
        )

    async def stop(self) -> None:
        """Stop the job processor and clean up."""
        if not self.running:
            return

        self.logger.info("Stopping job processor")
        self.running = False

        # Cancel worker task
        if self.worker_task and not self.worker_task.done():
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass

        # Cancel active jobs
        for _job_id, task in list(self.active_jobs.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Close HTTP client
        if self.client:
            await self.client.aclose()
            self.client = None

        self.logger.info("Job processor stopped")

    async def enqueue_jobs(self, jobs: list[dict[str, Any]]) -> None:
        """
        Enqueue jobs from heartbeat response.

        Args:
            jobs: List of job dispatch dictionaries from heartbeat
        """
        if not jobs:
            return

        for job_data in jobs:
            # Extract priority (negative for max-heap behavior)
            priority = -job_data.get("priority", 0)
            timestamp = time.time()

            # Add to queue (priority, timestamp for FIFO tie-breaking, job_data)
            await self.job_queue.put((priority, timestamp, job_data))

        self.logger.info(
            "Enqueued jobs",
            extra={"job_count": len(jobs), "queue_size": self.job_queue.qsize()},
        )

    async def _job_worker(self) -> None:
        """Background worker that processes jobs from the queue."""
        self.logger.info("Job worker started")

        while self.running:
            try:
                # Get job from queue (with timeout to allow clean shutdown)
                try:
                    priority, timestamp, job_data = await asyncio.wait_for(
                        self.job_queue.get(),
                        timeout=1.0,
                    )
                except TimeoutError:
                    continue

                # Execute job with concurrency control
                async with self.semaphore:
                    if not self.running:
                        break

                    await self._execute_job(job_data)

                # Mark task as done
                self.job_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception:
                self.logger.error("Error in job worker", exc_info=True)

        self.logger.info("Job worker stopped")

    async def _execute_job(self, job_data: dict[str, Any]) -> None:
        """
        Execute a single job.

        Args:
            job_data: Job dispatch data from heartbeat
        """
        job_id = str(job_data.get("job_id"))
        job_type: str | None = job_data.get("job_type")
        params = job_data.get("params", {})
        timeout = job_data.get("timeout_seconds", 300)

        self.logger.info(
            "Executing job",
            extra={"job_id": str(job_id), "job_type": job_type},
        )

        # Check if job type is registered
        job_class = self.job_registry.get(job_type) if job_type else None
        if not job_class:
            self.logger.error(
                "Unknown job type",
                extra={"job_type": job_type},
            )
            await self._submit_result(
                job_id,
                {
                    "status": "failed",
                    "result": None,
                    "error": f"Unknown job type: {job_type}",
                },
            )
            self.stats["jobs_failed_since_last"] += 1
            self.stats["total_failed"] += 1
            return

        # Create job handler
        try:
            job = job_class(job_id=job_id, params=params)
        except Exception as e:
            self.logger.error("Failed to create job handler", exc_info=True)
            await self._submit_result(
                job_id,
                {
                    "status": "failed",
                    "result": None,
                    "error": f"Failed to create job handler: {str(e)}",
                },
            )
            self.stats["jobs_failed_since_last"] += 1
            self.stats["total_failed"] += 1
            return

        # Execute job with timeout
        try:
            result_data = await asyncio.wait_for(job.run(), timeout=timeout)

            # Update stats
            if result_data["status"] == "completed":
                self.stats["jobs_completed_since_last"] += 1
                self.stats["total_completed"] += 1
            else:
                self.stats["jobs_failed_since_last"] += 1
                self.stats["total_failed"] += 1

            # Submit results to server
            await self._submit_result(job_id, result_data)

        except TimeoutError:
            self.logger.error(
                "Job timed out",
                extra={"job_id": str(job_id), "timeout_seconds": timeout},
            )
            await self._submit_result(
                job_id,
                {
                    "status": "failed",
                    "result": None,
                    "error": f"Job timed out after {timeout} seconds",
                },
            )
            self.stats["jobs_failed_since_last"] += 1
            self.stats["total_failed"] += 1

        except Exception as e:
            self.logger.error(
                "Job execution error",
                extra={"job_id": str(job_id)},
                exc_info=True,
            )
            await self._submit_result(
                job_id,
                {
                    "status": "failed",
                    "result": None,
                    "error": str(e),
                },
            )
            self.stats["jobs_failed_since_last"] += 1
            self.stats["total_failed"] += 1

    async def _submit_result(self, job_id: str, result_data: dict[str, Any]) -> None:
        """
        Submit job results to server.

        Args:
            job_id: Job UUID
            result_data: Result data (status, result, error)
        """
        push_url = self.config.get("push_url", "http://localhost:9000")

        # Extract base URL
        if "/api/v1/reports" in push_url:
            base_url = push_url.replace("/api/v1/reports", "")
        else:
            base_url = push_url

        # Build result submission URL
        result_url = f"{base_url}/api/v1/jobs/{job_id}/results"

        try:
            if not self.client:
                await self.start()
            assert self.client is not None

            response = await self.client.post(result_url, json=result_data)

            if response.status_code < 300:
                self.logger.info(
                    "Successfully submitted results for job",
                    extra={"job_id": str(job_id)},
                )
            else:
                self.logger.warning(
                    "Failed to submit results for job",
                    extra={
                        "job_id": str(job_id),
                        "status_code": response.status_code,
                    },
                )

        except Exception:
            self.logger.error(
                "Error submitting results for job",
                extra={"job_id": str(job_id)},
                exc_info=True,
            )

    def get_stats(self) -> dict[str, Any]:
        """
        Get job processor statistics.

        Returns:
            Dictionary with queue stats for heartbeat
        """
        stats = {
            "jobs_pending": self.job_queue.qsize(),
            "jobs_running": len(self.active_jobs),
            "jobs_completed_since_last": self.stats["jobs_completed_since_last"],
            "jobs_failed_since_last": self.stats["jobs_failed_since_last"],
        }

        # Reset per-heartbeat counters
        self.stats["jobs_completed_since_last"] = 0
        self.stats["jobs_failed_since_last"] = 0

        return stats
