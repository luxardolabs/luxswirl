"""Check tools: list, read, results, history and summary; create, edit, clone, enable, disable.

Each wraps the call the matching /api/v1/admin/checks route makes, with the caller's
role, so a viewer reads every credential masked and a synthetic check stays an
admin's, by the core services' own rules.
"""

from datetime import datetime
from typing import Any

from mcp.types import TextContent, Tool

from app.mcp.context import get_db
from app.mcp.formatting import json_response
from app.mcp.tools._base import (
    PAGE_PROPERTIES,
    body_schema,
    bool_arg,
    choice,
    page_args,
    principal,
)
from app.mcp.validators import (
    MCPValidationError,
    parse_body,
    validate_int_range,
    validate_optional_uuid,
    validate_uuid,
)
from app.models.enum_model import CheckType
from app.schemas.admin_check_schema import (
    AdminCheckClone,
    AdminCheckCreate,
    AdminCheckUpdate,
)
from app.services.core.admin_check_core_service import AdminCheckCoreService
from app.services.core.admin_check_result_core_service import (
    AdminCheckResultCoreService,
)

# The history and summary window, as on the admin API.
MAX_HOURS = 720

_CHECK_ID = {"check_id": {"type": "string", "description": "Check UUID"}}
_HOURS = {
    "hours": {
        "type": "integer",
        "description": f"How many hours back, 1-{MAX_HOURS}. Defaults to 24.",
    }
}


def _when(value: Any, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise MCPValidationError(f"{field_name} must be an ISO 8601 datetime")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise MCPValidationError(
            f"Invalid {field_name}: {value!r}. Use ISO 8601, e.g. 2026-10-27T12:00:00Z"
        ) from None


TOOLS: list[Tool] = [
    Tool(
        name="list_checks",
        description=(
            "One page of monitoring checks, filtered by agent, type, tag, enabled, or a "
            "name/target search. Returns each check's id, type, target and settings."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **PAGE_PROPERTIES,
                "agent_id": {
                    "type": "string",
                    "description": "Only this agent's checks",
                },
                "check_type": {
                    "type": "string",
                    "enum": [t.value for t in CheckType],
                    "description": "Only checks of this type",
                },
                "enabled_only": {
                    "type": "boolean",
                    "description": "Only enabled checks",
                },
                "tag": {"type": "string", "description": "Only checks with this tag"},
                "search": {
                    "type": "string",
                    "description": "Match the check's name or target",
                },
            },
        },
    ),
    Tool(
        name="get_check",
        description="One check: its type, target, schedule, alerts and settings.",
        inputSchema={
            "type": "object",
            "properties": _CHECK_ID,
            "required": ["check_id"],
        },
    ),
    Tool(
        name="get_check_results",
        description=(
            "One page of a check's raw results, newest first, in a since/until window "
            "(the last 24 hours by default, at most 31 days)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_CHECK_ID,
                **PAGE_PROPERTIES,
                "since": {"type": "string", "description": "ISO 8601 start"},
                "until": {
                    "type": "string",
                    "description": "ISO 8601 end; now if absent",
                },
            },
            "required": ["check_id"],
        },
    ),
    Tool(
        name="get_check_history",
        description=(
            "A check's results over the last hours, bucketed: success rate and latency "
            "per bucket. Use this for trends ('was it flapping overnight?')."
        ),
        inputSchema={
            "type": "object",
            "properties": {**_CHECK_ID, **_HOURS},
            "required": ["check_id"],
        },
    ),
    Tool(
        name="get_check_summary",
        description=(
            "A check's results over the last hours, counted (up, down, uptime, latency), "
            "and its latest result. Use this to answer 'is it up, and how has it been?'."
        ),
        inputSchema={
            "type": "object",
            "properties": {**_CHECK_ID, **_HOURS},
            "required": ["check_id"],
        },
    ),
    Tool(
        name="create_check",
        description=(
            "Create a check on an agent: the same body as POST /api/v1/admin/checks. "
            "A synthetic check is an admin's."
        ),
        inputSchema=body_schema(AdminCheckCreate),
    ),
    Tool(
        name="update_check",
        description=(
            "Change a check: `changes` is the same body as PATCH "
            "/api/v1/admin/checks/{id}, and a field left out is unchanged."
        ),
        inputSchema={
            "type": "object",
            "properties": {**_CHECK_ID, "changes": body_schema(AdminCheckUpdate)},
            "required": ["check_id", "changes"],
        },
    ),
    Tool(
        name="clone_check",
        description=(
            "Copy a check, optionally to another agent and with changes: `changes` is "
            "the same body as POST /api/v1/admin/checks/{id}/clone."
        ),
        inputSchema={
            "type": "object",
            "properties": {**_CHECK_ID, "changes": body_schema(AdminCheckClone)},
            "required": ["check_id"],
        },
    ),
    Tool(
        name="enable_check",
        description="Turn a check on: its agent runs it from its next config fetch.",
        inputSchema={
            "type": "object",
            "properties": _CHECK_ID,
            "required": ["check_id"],
        },
    ),
    Tool(
        name="disable_check",
        description="Turn a check off: its agent stops running it.",
        inputSchema={
            "type": "object",
            "properties": _CHECK_ID,
            "required": ["check_id"],
        },
    ),
]


async def handle_list_checks(arguments: dict[str, Any]) -> list[TextContent]:
    page, per_page = page_args(arguments)
    check_type = choice(arguments.get("check_type"), CheckType, "check_type")
    return json_response(
        await AdminCheckCoreService.list_checks(
            get_db(),
            principal().user.role,
            page=page,
            per_page=per_page,
            agent_id=validate_optional_uuid(arguments.get("agent_id"), "agent_id"),
            check_type=check_type.value if check_type else None,
            enabled_only=bool_arg(
                arguments.get("enabled_only"), "enabled_only", default=False
            ),
            tag=arguments.get("tag") or None,
            search=arguments.get("search") or None,
        )
    )


async def handle_get_check(arguments: dict[str, Any]) -> list[TextContent]:
    return json_response(
        await AdminCheckCoreService.get_check(
            get_db(),
            principal().user.role,
            validate_uuid(arguments.get("check_id"), "check_id"),
        )
    )


async def handle_get_check_results(arguments: dict[str, Any]) -> list[TextContent]:
    page, per_page = page_args(arguments)
    return json_response(
        await AdminCheckResultCoreService.list_results(
            get_db(),
            principal().user.role,
            validate_uuid(arguments.get("check_id"), "check_id"),
            since=_when(arguments.get("since"), "since"),
            until=_when(arguments.get("until"), "until"),
            page=page,
            per_page=per_page,
        )
    )


def _hours(arguments: dict[str, Any]) -> int:
    return validate_int_range(
        arguments.get("hours"), "hours", default=24, minimum=1, maximum=MAX_HOURS
    )


async def handle_get_check_history(arguments: dict[str, Any]) -> list[TextContent]:
    return json_response(
        await AdminCheckResultCoreService.history(
            get_db(),
            principal().user.role,
            validate_uuid(arguments.get("check_id"), "check_id"),
            _hours(arguments),
        )
    )


async def handle_get_check_summary(arguments: dict[str, Any]) -> list[TextContent]:
    return json_response(
        await AdminCheckResultCoreService.summary(
            get_db(),
            principal().user.role,
            validate_uuid(arguments.get("check_id"), "check_id"),
            _hours(arguments),
        )
    )


async def handle_create_check(arguments: dict[str, Any]) -> list[TextContent]:
    data = parse_body(AdminCheckCreate, arguments)
    return json_response(
        await AdminCheckCoreService.create_check(get_db(), principal().user.role, data)
    )


async def handle_update_check(arguments: dict[str, Any]) -> list[TextContent]:
    check_id = validate_uuid(arguments.get("check_id"), "check_id")
    data = parse_body(AdminCheckUpdate, arguments.get("changes") or {})
    return json_response(
        await AdminCheckCoreService.update_check(
            get_db(), principal().user.role, check_id, data
        )
    )


async def handle_clone_check(arguments: dict[str, Any]) -> list[TextContent]:
    check_id = validate_uuid(arguments.get("check_id"), "check_id")
    data = parse_body(AdminCheckClone, arguments.get("changes") or {})
    return json_response(
        await AdminCheckCoreService.clone_check(
            get_db(), principal().user.role, check_id, data
        )
    )


async def _set_enabled(
    arguments: dict[str, Any], *, enabled: bool
) -> list[TextContent]:
    return json_response(
        await AdminCheckCoreService.set_enabled(
            get_db(),
            principal().user.role,
            validate_uuid(arguments.get("check_id"), "check_id"),
            enabled=enabled,
        )
    )


async def handle_enable_check(arguments: dict[str, Any]) -> list[TextContent]:
    return await _set_enabled(arguments, enabled=True)


async def handle_disable_check(arguments: dict[str, Any]) -> list[TextContent]:
    return await _set_enabled(arguments, enabled=False)


HANDLERS = {
    "list_checks": handle_list_checks,
    "get_check": handle_get_check,
    "get_check_results": handle_get_check_results,
    "get_check_history": handle_get_check_history,
    "get_check_summary": handle_get_check_summary,
    "create_check": handle_create_check,
    "update_check": handle_update_check,
    "clone_check": handle_clone_check,
    "enable_check": handle_enable_check,
    "disable_check": handle_disable_check,
}
