"""
Base job handler class for agent-side job execution.

All job types inherit from BaseJob and implement the execute() method.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from shared.logger import get_logger


class BaseJob(ABC):
    """
    Base class for all job handlers.

    Job handlers execute specific types of work (network scans, port scans, etc.)
    and return structured results.
    """

    # Job type identifier (must be overridden in subclasses)
    job_type: str = "base"

    def __init__(self, job_id: str, params: dict[str, Any]):
        """
        Initialize job handler.

        Args:
            job_id: Unique job identifier (UUID)
            params: Job-specific parameters
        """
        self.job_id = job_id
        self.params = params
        self.logger = get_logger(f"luxswirl.jobs.{self.job_type}")

        # Execution state
        self.start_time: float | None = None
        self.end_time: float | None = None

    @abstractmethod
    async def execute(self) -> dict[str, Any]:
        """
        Execute the job and return results.

        This method must be implemented by subclasses.

        Returns:
            Dictionary with job results

        Raises:
            Exception: If job execution fails
        """

    async def run(self) -> dict[str, Any]:
        """
        Run the job with timing and error handling.

        Returns:
            Dictionary with result structure:
            {
                "status": "completed" | "failed",
                "result": {...} | None,
                "error": str | None
            }
        """
        self.logger.info(
            "Starting job",
            extra={"job_id": str(self.job_id), "job_type": self.job_type},
        )
        self.start_time = time.time()

        try:
            # Execute the job
            result = await self.execute()

            self.end_time = time.time()
            duration = self.end_time - self.start_time

            self.logger.info(
                "Job completed successfully",
                extra={
                    "job_id": str(self.job_id),
                    "duration_seconds": round(duration, 2),
                },
            )

            return {
                "status": "completed",
                "result": result,
                "error": None,
            }

        except asyncio.CancelledError:
            self.logger.warning(
                "Job was cancelled",
                extra={"job_id": str(self.job_id)},
            )
            raise

        except Exception as e:
            self.end_time = time.time()
            duration = self.end_time - self.start_time if self.start_time else 0

            self.logger.error(
                "Job failed",
                extra={
                    "job_id": str(self.job_id),
                    "duration_seconds": round(duration, 2),
                },
                exc_info=True,
            )

            return {
                "status": "failed",
                "result": None,
                "error": str(e),
            }

    @property
    def duration(self) -> float | None:
        """Get job execution duration in seconds."""
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return None

    def validate_params(self, required: list[str]) -> None:
        """
        Validate that required parameters are present.

        Args:
            required: List of required parameter names

        Raises:
            ValueError: If any required parameter is missing
        """
        missing = [p for p in required if p not in self.params]
        if missing:
            raise ValueError(f"Missing required parameters: {', '.join(missing)}")

    def get_param(self, name: str, default: Any = None) -> Any:
        """
        Get a parameter value with optional default.

        Args:
            name: Parameter name
            default: Default value if not found

        Returns:
            Parameter value or default
        """
        return self.params.get(name, default)

    def __repr__(self) -> str:
        """String representation."""
        return f"<{self.__class__.__name__}(job_id={self.job_id}, type={self.job_type})>"
