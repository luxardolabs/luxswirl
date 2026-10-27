"""
Custom exceptions for LuxSwirl application.
"""

from typing import Any

from pydantic import ValidationError


def validation_message(exc: ValidationError) -> str:
    """A schema refusal as `<field>: <reason>` per error, without pydantic's input echo.

    An error on the whole model (no field) is its reason alone.
    """
    parts = []
    for err in exc.errors(include_url=False, include_input=False):
        field = ".".join(str(part) for part in err["loc"])
        parts.append(f"{field}: {err['msg']}" if field else err["msg"])
    return "; ".join(parts)


def value_error_message(exc: ValueError) -> str:
    """The text a page shows for a refusal: a schema's as field and reason, else its own."""
    if isinstance(exc, ValidationError):
        return validation_message(exc)
    return str(exc)


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


class SchedulerJobNotFoundException(NotFoundException):
    """Raised when no scheduled job has the key."""

    def __init__(self, job_key: str):
        super().__init__(
            message=f"Job {job_key} not found", details={"job_key": job_key}
        )


class SchedulerJobBusyException(LuxSwirlException):
    """Raised when a job is asked to run while a run of it holds its lease."""

    status_code = 409
    error_code = "JOB_RUNNING"

    def __init__(self, job_key: str):
        super().__init__(
            message=f"Job {job_key} is running; try again when it ends",
            details={"job_key": job_key},
        )


class UserNotFoundException(NotFoundException):
    """Raised when no user has the id."""

    def __init__(self, user_id: object):
        super().__init__(
            message=f"User not found: {user_id}", details={"user_id": str(user_id)}
        )


class LastActiveAdminException(LuxSwirlException):
    """Raised by a change that would leave no active admin to sign in."""

    status_code = 409
    error_code = "LAST_ACTIVE_ADMIN"


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
