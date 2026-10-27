"""MCP authorization layer.

Ported from LuxWX's `app/mcp/authorization/` (itself from LuxPM's
`app/core/authorization/`). The scope vocabulary is shared with the admin API and
lives in `app/core/authorization`:

- `registry.py` — every tool → (required scopes, role); unregistered = DENY
- `gates.py`    — `authorize_tool_call()`, the single chokepoint before dispatch
- `errors.py`   — typed denials
"""

from app.mcp.authorization.errors import AuthorizationError
from app.mcp.authorization.gates import authorize_tool_call, may_call
from app.mcp.authorization.registry import (
    META_TOOLS,
    REGISTRY,
    ToolGate,
    get_gate,
    is_meta_tool,
    registered_tools,
)

__all__ = [
    "META_TOOLS",
    "REGISTRY",
    "AuthorizationError",
    "ToolGate",
    "authorize_tool_call",
    "get_gate",
    "is_meta_tool",
    "may_call",
    "registered_tools",
]
