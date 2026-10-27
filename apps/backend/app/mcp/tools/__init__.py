"""MCP tool registry — assembles definitions and handlers from the family modules.

Port of LuxWX's `app/mcp/tools/__init__.py`. Each module holds one family's tools
(TOOLS) and their handlers (HANDLERS); `list_tools` filters them per token by the
authorization registry, and a tool missing from it is never callable.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from mcp.types import TextContent, Tool

from app.mcp.tools import agents, alerts, checks, jobs, meta
from app.mcp.tools._base import ToolError

_MODULES = [meta, checks, agents, alerts, jobs]

ALL_TOOLS: list[Tool] = []
for _mod in _MODULES:
    ALL_TOOLS.extend(_mod.TOOLS)

ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, list[TextContent]]]

HANDLERS: dict[str, ToolHandler] = {}
for _mod in _MODULES:
    HANDLERS.update(_mod.HANDLERS)

__all__ = ["ALL_TOOLS", "HANDLERS", "ToolError", "ToolHandler"]
