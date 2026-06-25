"""
Custom async scheduler service - no external dependencies.

Single-tenant async scheduler for LuxSwirl. Uses the project's logger and
database session patterns.
"""

import asyncio
import random
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.scheduler_crud import JobConfigurationCRUD, JobExecutionCRUD
from app.db import worker_session
from app.models.enum_model import SchedulerExecutionStatus
from app.models.scheduler_model import JobConfiguration, JobExecution

# Import job functions from cleanup + monitoring core services
from app.services.core.cleanup_core_service import (
    cleanup_check_artifacts,
    cleanup_notification_logs,
    cleanup_old_job_executions,
    cleanup_old_sessions,
    cleanup_stale_agents,
)
from app.services.core.monitoring_core_service import (
    collect_database_metrics,
    collect_operational_metrics,
)

logger = get_logger("luxswirl.scheduler")


class SchedulerCoreService:
    """Custom async scheduler with PostgreSQL persistence."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self.job_functions: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}
        self._register_job_functions()

    def _register_job_functions(self) -> None:
        """Register all available job functions."""
        self.job_functions = {
            # Cleanup jobs
            "cleanup_old_job_executions": cleanup_old_job_executions,
            "cleanup_notification_logs": cleanup_notification_logs,
            "cleanup_stale_agents": cleanup_stale_agents,
            "cleanup_check_artifacts": cleanup_check_artifacts,
            "cleanup_old_sessions": cleanup_old_sessions,
            # Monitoring jobs
            "collect_database_metrics": collect_database_metrics,
            "collect_operational_metrics": collect_operational_metrics,
        }

    async def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info("Async scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Async scheduler stopped")

    async def _scheduler_loop(self) -> None:
        """Main scheduler loop - runs every 10 seconds."""
        while self._running:
            try:
                await self._process_due_jobs()
                await asyncio.sleep(10)  # Poll every 10 seconds
            except Exception as e:
                logger.error("Scheduler loop error", extra={"error": str(e)})
                await asyncio.sleep(30)  # Backoff on error

    async def _process_due_jobs(self) -> None:
        """
        Process all jobs that are due to run.

        Uses SELECT FOR UPDATE SKIP LOCKED for distributed locking.
        """
        dispatch: list[tuple[str, UUID]] = []
        async with worker_session() as db:
            now = datetime.now(UTC)

            # Select due jobs with expired or no lease
            jobs = await JobConfigurationCRUD.get_due_jobs(db, now)

            for job in jobs:
                # Acquire lease
                lease_token = uuid4()
                lease_duration = job.max_runtime_seconds * 2
                job.lease_token = lease_token
                job.lease_expires_at = now + timedelta(seconds=lease_duration)
                dispatch.append((job.job_key, lease_token))

        # worker_session commits the leases on clean block exit above. Dispatch
        # only after that — _execute_job opens its own session and re-verifies
        # each lease.
        for job_key, lease_token in dispatch:
            asyncio.create_task(self._execute_job(job_key, lease_token))

    async def _execute_job(self, job_key: str, lease_token: UUID) -> None:
        """Execute a single job."""
        async with worker_session() as db:
            # Verify we still hold the lease
            job = await JobConfigurationCRUD.get_by_job_key_with_lease(db, job_key, lease_token)

            if not job:
                logger.warning("Lost lease for job", extra={"job_key": job_key})
                return

            # Create execution record
            execution = await JobExecutionCRUD.create_execution(
                db,
                job_key=job_key,
                job_name=job.display_name,
                category=job.category,
                started_at=datetime.now(UTC),
                status="running",
            )

            try:
                # Get the function
                function_name = job.function_name

                if function_name in self.job_functions:
                    func = self.job_functions[function_name]

                    # Execute with timeout
                    result = await asyncio.wait_for(
                        func(**(job.parameters or {})),
                        timeout=job.max_runtime_seconds,
                    )
                else:
                    raise Exception(f"Function '{function_name}' not found")

                # Success - update execution
                execution.completed_at = datetime.now(UTC)
                execution.duration_seconds = (
                    execution.completed_at - execution.started_at
                ).total_seconds()
                execution.output = result

                # Check if job returned errors - use WARNING status
                errors = result.get("errors") if isinstance(result, dict) else None
                has_errors = False
                if errors is not None:
                    has_errors = len(errors) > 0 if isinstance(errors, list) else errors > 0
                final_status = (
                    SchedulerExecutionStatus.WARNING
                    if has_errors
                    else SchedulerExecutionStatus.SUCCESS
                )
                execution.status = final_status

                # Reset retry count on success
                job.retry_count = 0

                # Calculate next run time
                job.next_run_at = self._calculate_next_run(job)

                # Update stats
                job.last_run_at = datetime.now(UTC)
                job.last_status = final_status
                job.total_runs += 1

                logger.info(
                    "Job completed" + (" with errors" if has_errors else " successfully"),
                    extra={"job_key": job_key, "duration": execution.duration_seconds},
                )

            except TimeoutError:
                execution.status = SchedulerExecutionStatus.FAILED
                execution.error_message = f"Job timed out after {job.max_runtime_seconds} seconds"
                execution.completed_at = datetime.now(UTC)
                await self._handle_job_failure(db, job, execution.error_message)
                logger.error("Job timed out", extra={"job_key": job_key})

            except Exception as e:
                execution.status = SchedulerExecutionStatus.FAILED
                execution.error_message = str(e)
                execution.completed_at = datetime.now(UTC)
                await self._handle_job_failure(db, job, str(e))
                logger.error(
                    "Job failed",
                    extra={"job_key": job_key, "error": str(e)},
                    exc_info=True,
                )

            finally:
                # Clear lease — worker_session commits the whole unit on exit.
                job.lease_token = None
                job.lease_expires_at = None

    async def _handle_job_failure(
        self, db: AsyncSession, job: JobConfiguration, error: str
    ) -> None:
        """
        Handle job failure with retries.

        Retry behavior:
        - retry_limit = 0: Never auto-disable (infinite retries, critical jobs)
        - retry_limit > 0: Disable after retry_limit consecutive failures
        """
        job.retry_count += 1
        job.last_status = SchedulerExecutionStatus.FAILED
        job.failed_runs += 1

        # retry_limit = 0 means "never auto-disable" (infinite retries)
        if job.retry_limit == 0 or job.retry_count <= job.retry_limit:
            # Exponential backoff
            backoff = job.backoff_seconds * (2 ** (job.retry_count - 1))
            # Cap backoff at 1 hour for infinite retry jobs
            if job.retry_limit == 0:
                backoff = min(backoff, 3600)
            job.next_run_at = datetime.now(UTC) + timedelta(seconds=backoff)
            logger.info(
                "Job will retry",
                extra={
                    "job_key": job.job_key,
                    "backoff_seconds": backoff,
                    "retry_count": job.retry_count,
                    "retry_limit": job.retry_limit,
                },
            )
        else:
            # Max retries exceeded - disable job
            job.enabled = False
            logger.error(
                "Job disabled after max failures",
                extra={"job_key": job.job_key, "retry_limit": job.retry_limit},
            )

    def _calculate_next_run(self, job: JobConfiguration) -> datetime | None:
        """
        Calculate the next run time for a job.

        Cron expressions are interpreted in the job's configured timezone,
        then converted to UTC for storage.
        """
        now_utc = datetime.now(UTC)

        if job.trigger_type == "interval":
            # Fixed-rate scheduling: calculate from original scheduled time
            interval = timedelta(seconds=job.interval_seconds or 60)

            # Start from when the job was SUPPOSED to run
            base_time = job.next_run_at or now_utc
            next_run = base_time + interval

            # If we're behind, advance to next valid slot
            while next_run <= now_utc:
                next_run += interval

            # Apply jitter if configured
            if job.jitter_ms > 0:
                jitter = random.randint(-job.jitter_ms, job.jitter_ms) / 1000
                next_run += timedelta(seconds=jitter)

            return next_run

        elif job.trigger_type == "cron":
            # Parse cron expression in LOCAL time, then convert to UTC
            if job.cron_expression:
                local_tz = ZoneInfo(job.timezone or "UTC")
                now_local = now_utc.astimezone(local_tz)

                parts = job.cron_expression.split()
                if len(parts) >= 5:
                    minute, hour, day, month, day_of_week = parts[:5]

                    # Handle hourly jobs (e.g., "30 * * * *")
                    if day == "*" and month == "*" and day_of_week == "*":
                        if minute != "*" and hour == "*":
                            target_minute = int(minute)
                            next_run_local = now_local.replace(
                                minute=target_minute, second=0, microsecond=0
                            )
                            if next_run_local <= now_local:
                                next_run_local += timedelta(hours=1)
                            return next_run_local.astimezone(UTC)

                        # Handle daily jobs (e.g., "0 2 * * *")
                        if minute != "*" and hour != "*":
                            target_hour = int(hour)
                            target_minute = int(minute)
                            next_run_local = now_local.replace(
                                hour=target_hour,
                                minute=target_minute,
                                second=0,
                                microsecond=0,
                            )
                            if next_run_local <= now_local:
                                next_run_local += timedelta(days=1)
                            return next_run_local.astimezone(UTC)

            # Default fallback - run tomorrow at same time
            return now_utc + timedelta(days=1)

        else:
            # Manual jobs don't schedule
            if job.trigger_type == "manual":
                return None
            # Date trigger - disable after running
            job.enabled = False
            return now_utc + timedelta(days=365)

    # =========================================================================
    # Admin API methods
    # =========================================================================

    async def get_all_jobs(self) -> list[JobConfiguration]:
        """Get all job configurations."""
        async with worker_session() as db:
            return await JobConfigurationCRUD.get_all_ordered(db)

    async def toggle_job(self, job_key: str) -> JobConfiguration:
        """
        Toggle a job's enabled status.

        Args:
            job_key: Job ID to toggle

        Returns:
            Updated JobConfiguration

        Raises:
            ValueError: If job not found
        """
        async with worker_session() as db:
            job = await JobConfigurationCRUD.get_by_job_key(db, job_key)

            if not job:
                raise ValueError(f"Job {job_key} not found")

            job.enabled = not job.enabled
            await db.flush()
            await db.refresh(job)

            logger.info("Toggled job", extra={"job_key": job_key, "enabled": job.enabled})
            return job

    async def execute_job_synchronously(self, job_key: str) -> dict:
        """
        Execute a job synchronously (wait for completion).

        Used for manual triggering from admin panel.

        Args:
            job_key: Job ID to run

        Returns:
            Dict with execution result

        Raises:
            ValueError: If job not found
        """
        async with worker_session() as db:
            job = await JobConfigurationCRUD.get_by_job_key(db, job_key)

            if not job:
                raise ValueError(f"Job {job_key} not found")

            logger.info(
                "Synchronously executing job",
                extra={"job_key": job_key, "display_name": job.display_name},
            )

            # Create execution record
            execution = await JobExecutionCRUD.create_execution(
                db,
                job_key=job_key,
                job_name=job.display_name,
                category=job.category,
                started_at=datetime.now(UTC),
                status="running",
            )

            try:
                function_name = job.function_name

                if function_name in self.job_functions:
                    func = self.job_functions[function_name]
                    result = await asyncio.wait_for(
                        func(**(job.parameters or {})),
                        timeout=job.max_runtime_seconds,
                    )
                else:
                    raise Exception(f"Function '{function_name}' not found")

                # Success
                execution.completed_at = datetime.now(UTC)
                execution.duration_seconds = (
                    execution.completed_at - execution.started_at
                ).total_seconds()
                execution.output = result

                errors = result.get("errors") if isinstance(result, dict) else None
                has_errors = False
                if errors is not None:
                    has_errors = len(errors) > 0 if isinstance(errors, list) else errors > 0
                final_status = (
                    SchedulerExecutionStatus.WARNING
                    if has_errors
                    else SchedulerExecutionStatus.SUCCESS
                )
                execution.status = final_status

                # Update job stats
                job.last_run_at = datetime.now(UTC)
                job.last_status = final_status
                job.total_runs += 1
                job.retry_count = 0
                job.next_run_at = self._calculate_next_run(job)

                return {
                    "status": final_status,
                    "execution_id": str(execution.id),
                    "duration_seconds": execution.duration_seconds,
                }

            except TimeoutError:
                execution.status = SchedulerExecutionStatus.FAILED
                execution.error_message = f"Job timed out after {job.max_runtime_seconds} seconds"
                execution.completed_at = datetime.now(UTC)
                job.last_status = SchedulerExecutionStatus.FAILED
                job.failed_runs += 1
                raise

            except Exception as e:
                execution.status = SchedulerExecutionStatus.FAILED
                execution.error_message = str(e)
                execution.completed_at = datetime.now(UTC)
                job.last_status = SchedulerExecutionStatus.FAILED
                job.failed_runs += 1
                raise

    async def reset_job(self, job_key: str) -> JobConfiguration:
        """
        Reset a job's retry state and re-enable it.

        Args:
            job_key: Job ID to reset

        Returns:
            Updated JobConfiguration

        Raises:
            ValueError: If job not found
        """
        async with worker_session() as db:
            job = await JobConfigurationCRUD.get_by_job_key(db, job_key)

            if not job:
                raise ValueError(f"Job {job_key} not found")

            old_enabled = job.enabled

            job.retry_count = 0
            job.failed_runs = 0
            job.enabled = True

            # Schedule next run if it was disabled
            if not old_enabled:
                job.next_run_at = self._calculate_next_run(job)

            await db.flush()
            await db.refresh(job)

            logger.info("Job reset", extra={"job_key": job_key})
            return job

    async def get_job_history(
        self, job_key: str, limit: int = 50
    ) -> tuple[JobConfiguration, list[JobExecution]]:
        """
        Get job configuration and execution history.

        Args:
            job_key: Job ID
            limit: Max executions to return

        Returns:
            Tuple of (job, executions)

        Raises:
            ValueError: If job not found
        """
        async with worker_session() as db:
            job = await JobConfigurationCRUD.get_by_job_key(db, job_key)

            if not job:
                raise ValueError(f"Job {job_key} not found")

            executions = await JobExecutionCRUD.get_by_job_key(db, job_key, limit=limit)
            return job, executions


# Global instance
scheduler_service = SchedulerCoreService()
