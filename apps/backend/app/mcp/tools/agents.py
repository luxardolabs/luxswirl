"""Agent tools: list and read. The agents that run the checks, and how they are doing."""

from typing import Any

from mcp.types import TextContent, Tool

from app.mcp.context import get_db
from app.mcp.formatting import json_response
from app.mcp.tools._base import PAGE_PROPERTIES, bool_arg, page_args, text_arg
from app.mcp.validators import validate_uuid
from app.services.core.admin_agent_core_service import AdminAgentCoreService

TOOLS: list[Tool] = [
    Tool(
        name="list_agents",
        description=(
            "One page of agents: their status, last heartbeat, version and tags. "
            "Archived agents only with archived=true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **PAGE_PROPERTIES,
                "search": {
                    "type": "string",
                    "maxLength": 255,
                    "description": "Match the agent's name, hostname or address",
                },
                "archived": {"type": "boolean", "description": "List archived agents"},
                "exclude_pending": {
                    "type": "boolean",
                    "description": "Leave out agents awaiting approval",
                },
            },
        },
    ),
    Tool(
        name="get_agent",
        description="One agent: its status, approval, last heartbeat and self-metrics.",
        inputSchema={
            "type": "object",
            "properties": {"agent_id": {"type": "string", "description": "Agent UUID"}},
            "required": ["agent_id"],
        },
    ),
]


async def handle_list_agents(arguments: dict[str, Any]) -> list[TextContent]:
    page, per_page = page_args(arguments)
    return json_response(
        await AdminAgentCoreService.list_agents(
            get_db(),
            page=page,
            per_page=per_page,
            search=text_arg(arguments.get("search"), "search", max_length=255),
            archived=bool_arg(arguments.get("archived"), "archived", default=False),
            exclude_pending=bool_arg(
                arguments.get("exclude_pending"), "exclude_pending", default=False
            ),
        )
    )


async def handle_get_agent(arguments: dict[str, Any]) -> list[TextContent]:
    return json_response(
        await AdminAgentCoreService.get_agent(
            get_db(), validate_uuid(arguments.get("agent_id"), "agent_id")
        )
    )


HANDLERS = {"list_agents": handle_list_agents, "get_agent": handle_get_agent}
