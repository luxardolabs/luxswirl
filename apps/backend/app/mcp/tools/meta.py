"""The meta tool: who this token is, and what it may do (LuxPM's and LuxWX's `whoami`)."""

from typing import Any

from mcp.types import TextContent, Tool

from app.mcp.authorization import REGISTRY, may_call
from app.mcp.formatting import json_response
from app.mcp.tools._base import principal

TOOLS: list[Tool] = [
    Tool(
        name="whoami",
        description=(
            "Return this API token's effective access: its user and role, its scopes, "
            "and the tools it may call. Call this first in a session to learn what "
            "this token can do."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
]


async def handle_whoami(arguments: dict[str, Any]) -> list[TextContent]:
    caller = principal()
    return json_response(
        {
            "user_id": caller.user.id,
            "username": caller.user.username,
            "role": caller.user.role,
            "token_id": caller.token_id,
            "token_name": caller.token_name,
            "token_expires_at": caller.token_expires_at,
            "scopes": list(caller.scopes),
            "tools": sorted(name for name in REGISTRY if may_call(caller, name)),
        }
    )


HANDLERS = {"whoami": handle_whoami}
