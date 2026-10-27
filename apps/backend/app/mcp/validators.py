"""Input validation for MCP tool handlers.

Port of LuxWX's `app/mcp/validators.py`: the UUID and integer-range checks,
verbatim. Agent-supplied arguments are untrusted: they arrive from an LLM that
may hallucinate ids or ask for a year of results. Every handler validates
before touching a service, and raises MCPValidationError with a message naming
what is valid so the agent can self-correct on the next turn.

A tool whose arguments are a whole admin-API body validates them with that
route's own schema instead (`parse_body`), so the two surfaces refuse alike.
"""

import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from app.core.error_summary import validation_summary

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class MCPValidationError(Exception):
    """Raised when agent-supplied arguments are unusable. Surfaced back to the agent."""


def validate_uuid(value: Any, field_name: str = "id") -> UUID:
    """Parse a UUID argument."""
    if not value:
        raise MCPValidationError(f"{field_name} is required")
    if not isinstance(value, str) or not _UUID_RE.match(value):
        raise MCPValidationError(f"Invalid UUID for {field_name}: {value!r}")
    return UUID(value)


def validate_optional_uuid(value: Any, field_name: str) -> UUID | None:
    """A UUID argument the tool may go without."""
    if value in (None, ""):
        return None
    return validate_uuid(value, field_name)


def validate_int_range(
    value: Any, field_name: str, *, default: int, minimum: int, maximum: int
) -> int:
    """An integer argument within [minimum, maximum], or the default when absent."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise MCPValidationError(f"{field_name} must be an integer")
    if not minimum <= value <= maximum:
        raise MCPValidationError(
            f"{field_name} must be between {minimum} and {maximum}, got {value}"
        )
    return value


def parse_body[T: BaseModel](schema: type[T], arguments: dict[str, Any]) -> T:
    """`arguments` held to an admin-API body schema; its errors, without inputs."""
    try:
        return schema.model_validate(arguments)
    except ValidationError as e:
        raise MCPValidationError(validation_summary(e)) from None
