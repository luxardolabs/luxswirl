"""What every MCP tool module shares: the caller, typed errors, argument helpers.

Handlers are thin wrappers over the admin API's own core services, so a tool does
exactly what its admin-API route does, under the same rules: no business logic
lives here. TRANSACTIONS: handlers are request-scoped CONSUMERS and never commit;
the MCP auth middleware owns the transaction.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from app.mcp.context import get_principal
from app.mcp.validators import MCPValidationError, validate_int_range
from app.schemas.admin_page_schema import MAX_PAGE
from app.schemas.api_token_schema import ApiPrincipal


class ToolError(Exception):
    """A tool failed in a way the agent should see as a plain message."""


def principal() -> ApiPrincipal:
    """The caller: the token's user, role and scopes, bound by the auth middleware."""
    caller = get_principal()
    if caller is None:
        raise RuntimeError("principal missing from MCP context")
    return caller


def page_args(arguments: dict[str, Any]) -> tuple[int, int]:
    """(page, per_page), within the admin API's own bounds."""
    page = validate_int_range(
        arguments.get("page"), "page", default=1, minimum=1, maximum=MAX_PAGE
    )
    per_page = validate_int_range(
        arguments.get("per_page"), "per_page", default=50, minimum=1, maximum=200
    )
    return page, per_page


def choice[E: StrEnum](value: Any, enum: type[E], field_name: str) -> E | None:
    """One of `enum`'s values, or None when absent; a bad one names the valid ones."""
    if value in (None, ""):
        return None
    try:
        return enum(value)
    except ValueError:
        valid = ", ".join(member.value for member in enum)
        raise MCPValidationError(
            f"Invalid {field_name}: {value!r}. Must be one of: {valid}"
        ) from None


def text_arg(value: Any, field_name: str, *, max_length: int) -> str | None:
    """A free-text argument, at most `max_length` characters (the route's own bound)."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise MCPValidationError(f"{field_name} must be a string")
    if len(value) > max_length:
        raise MCPValidationError(
            f"{field_name} is at most {max_length} characters, got {len(value)}"
        )
    return value


def bool_arg(value: Any, field_name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise MCPValidationError(f"{field_name} must be true or false")
    return value


def body_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The JSON Schema of an admin-API body, as a tool's input schema."""
    return schema.model_json_schema()


PAGE_PROPERTIES: dict[str, Any] = {
    "page": {"type": "integer", "description": "Page number, from 1. Defaults to 1."},
    "per_page": {
        "type": "integer",
        "description": "Rows per page, 1-200. Defaults to 50.",
    },
}
