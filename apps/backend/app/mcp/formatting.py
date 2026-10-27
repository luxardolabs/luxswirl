"""Shared formatting helpers for MCP tool responses.

Port of LuxWX's `app/mcp/formatting.py` (itself LuxPM's), verbatim but for this
docstring. Every payload is JSON so the calling agent parses rather than scrapes.
"""

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from mcp.types import TextContent


def _fallback(value: Any) -> Any:
    """Serialise the types our services return that json doesn't handle natively.

    Order matters. Dataclasses and enums MUST be handled before the `str()` catch-all,
    or a dataclass reaches the agent as its Python repr, which it cannot parse. The
    catch-all stays as a last resort so an unexpected type degrades to a readable
    string instead of failing the whole response.
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "model_dump"):  # pydantic
        return value.model_dump(mode="json")
    return str(value)


def text_response(text: str) -> list[TextContent]:
    """Wrap a plain string as an MCP text response."""
    return [TextContent(type="text", text=text)]


def json_response(payload: Any) -> list[TextContent]:
    """Wrap a structured payload as pretty-printed JSON."""
    return [
        TextContent(type="text", text=json.dumps(payload, indent=2, default=_fallback))
    ]
