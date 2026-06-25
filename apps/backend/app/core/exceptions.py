"""
Custom exceptions for LuxSwirl application.
"""

from typing import Any


class LuxSwirlException(Exception):
    """Base exception for all LuxSwirl errors."""

    status_code = 500
    error_code = "INTERNAL_ERROR"

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class NotFoundException(LuxSwirlException):
    """Raised when a resource is not found."""

    status_code = 404
    error_code = "NOT_FOUND"


class AgentNotFoundException(NotFoundException):
    """Raised when an agent is not found."""

    def __init__(self, agent_id: str):
        super().__init__(
            message=f"Agent not found: {agent_id}",
            details={"agent_id": agent_id},
        )


class CheckNotFoundException(NotFoundException):
    """Raised when a check is not found."""

    def __init__(self, agent_id: str, check_name: str):
        super().__init__(
            message=f"Check not found: {agent_id}:{check_name}",
            details={"agent_id": agent_id, "check_name": check_name},
        )


class StatusPageNotFoundException(NotFoundException):
    """Raised when a status page is not found."""

    def __init__(self, identifier: str | int):
        super().__init__(
            message=f"Status page not found: {identifier}",
            details={"identifier": identifier},
        )


class DuplicateResourceException(LuxSwirlException):
    """Raised when attempting to create a duplicate resource."""

    status_code = 409
    error_code = "DUPLICATE_RESOURCE"


class ValidationException(LuxSwirlException):
    """Raised when validation fails."""

    status_code = 422
    error_code = "VALIDATION_ERROR"


class DatabaseException(LuxSwirlException):
    """Raised when database operations fail."""

    status_code = 500
    error_code = "DATABASE_ERROR"


class AuthenticationException(LuxSwirlException):
    """Raised when authentication fails."""

    status_code = 401
    error_code = "AUTHENTICATION_FAILED"


class AuthorizationException(LuxSwirlException):
    """Raised when authorization fails."""

    status_code = 403
    error_code = "AUTHORIZATION_FAILED"
