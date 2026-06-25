"""
Base Check Module - Defines the interface for all health checks in LuxSwirl.
"""

import time
from abc import ABC, abstractmethod
from typing import Any


class BaseCheck(ABC):
    """Base abstract class that all health checks must inherit from."""

    def __init__(self, config: dict[str, Any]):
        """Initialize a check with its configuration.

        Args:
            config: The check configuration dictionary

        Raises:
            ValueError: If required configuration fields are missing
        """
        self.validate_config(config)

        self.check_id = config.get("check_id")  # UUID from server
        self.name = config.get("name", "unnamed_check")  # For logging
        self.config = config
        self.last_run_time: float | None = None
        self.last_result: dict[str, Any] | None = None

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate the check configuration.

        Args:
            config: The check configuration to validate

        Raises:
            ValueError: If required configuration fields are missing
        """
        if "check_id" not in config:
            raise ValueError("Check configuration must include a 'check_id'")

        if "target" not in config:
            raise ValueError(f"Check {config.get('name', 'unknown')} must have a 'target'")

        if "check_type" not in config:
            raise ValueError(f"Check {config.get('name', 'unknown')} must have a 'check_type'")

    @abstractmethod
    async def run(self) -> dict[str, Any]:
        """Execute the health check.

        This method must be implemented by all check classes. It should execute
        the actual health check logic and return a result dictionary.

        Returns:
            A dictionary containing at minimum:
                - check_id: The UUID of the check
                - display_name: The name of the check (for logging)
                - check_type: The type of check
                - target: The target that was checked
                - success: Boolean indicating success or failure
                - latency_ms: The check latency in milliseconds (or None)
        """

    def start_timer(self) -> float:
        """Start a timer for measuring check latency.

        Returns:
            The start time as a floating point value
        """
        return time.perf_counter()

    def stop_timer(self, start_time: float) -> float:
        """Stop the timer and calculate latency.

        Args:
            start_time: The start time from start_timer()

        Returns:
            The latency in milliseconds, rounded to 2 decimal places
        """
        latency = time.perf_counter() - start_time
        return round(latency * 1000, 2)  # Convert to ms and round

    def create_result(
        self,
        success: bool,
        latency_ms: float | None = None,
        error: str | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """Create a standardized result dictionary.

        Args:
            success: Whether the check was successful
            latency_ms: The latency in milliseconds, if available
            error: An error message, if applicable
            **kwargs: Additional fields to include in the result

        Returns:
            A dictionary with the check result
        """
        result = {
            "check_id": str(self.check_id),  # UUID as string
            "display_name": self.name,  # For logging only
            "check_type": self.config["check_type"],
            "target": self.config["target"],
            "success": success,
            "latency_ms": latency_ms,
        }

        if error:
            result["error"] = error

        # Add any additional fields
        result.update(kwargs)

        # Store the result
        self.last_result = result
        self.last_run_time = time.time()

        return result
