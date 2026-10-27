"""Alert tools: list and read alert rules; snooze and unsnooze one on a check."""

from typing import Any

from mcp.types import TextContent, Tool

from app.mcp.context import get_db
from app.mcp.formatting import json_response, text_response
from app.mcp.tools._base import PAGE_PROPERTIES, page_args, principal
from app.mcp.validators import MCPValidationError, validate_int_range, validate_uuid
from app.services.core.admin_alert_core_service import AdminAlertCoreService

_ALERT_AND_CHECK = {
    "alert_id": {"type": "string", "description": "Alert UUID"},
    "check_id": {"type": "string", "description": "Check UUID"},
}


def _optional_bool(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise MCPValidationError(f"{field_name} must be true or false")
    return value


TOOLS: list[Tool] = [
    Tool(
        name="list_alerts",
        description="One page of alert rules: their trigger, providers and checks.",
        inputSchema={
            "type": "object",
            "properties": {
                **PAGE_PROPERTIES,
                "is_enabled": {
                    "type": "boolean",
                    "description": "Only enabled (or not)",
                },
                "is_global": {
                    "type": "boolean",
                    "description": "Only global alerts (or not)",
                },
            },
        },
    ),
    Tool(
        name="get_alert",
        description="One alert rule, with its notification provider and check ids.",
        inputSchema={
            "type": "object",
            "properties": {"alert_id": {"type": "string", "description": "Alert UUID"}},
            "required": ["alert_id"],
        },
    ),
    Tool(
        name="snooze_alert_check",
        description=(
            "Hold back an alert's notifications for one check, for `minutes` (15 by "
            "default); a running snooze is extended, to at most a day."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_ALERT_AND_CHECK,
                "minutes": {
                    "type": "integer",
                    "description": "1-1440. Defaults to 15.",
                },
            },
            "required": ["alert_id", "check_id"],
        },
    ),
    Tool(
        name="unsnooze_alert_check",
        description="End a snooze: the alert notifies for the check again.",
        inputSchema={
            "type": "object",
            "properties": _ALERT_AND_CHECK,
            "required": ["alert_id", "check_id"],
        },
    ),
]


async def handle_list_alerts(arguments: dict[str, Any]) -> list[TextContent]:
    page, per_page = page_args(arguments)
    return json_response(
        await AdminAlertCoreService.list_alerts(
            get_db(),
            page=page,
            per_page=per_page,
            is_enabled=_optional_bool(arguments.get("is_enabled"), "is_enabled"),
            is_global=_optional_bool(arguments.get("is_global"), "is_global"),
        )
    )


async def handle_get_alert(arguments: dict[str, Any]) -> list[TextContent]:
    return json_response(
        await AdminAlertCoreService.get_alert(
            get_db(), validate_uuid(arguments.get("alert_id"), "alert_id")
        )
    )


async def handle_snooze_alert_check(arguments: dict[str, Any]) -> list[TextContent]:
    return json_response(
        await AdminAlertCoreService.snooze(
            get_db(),
            principal().user.username,
            validate_uuid(arguments.get("alert_id"), "alert_id"),
            validate_uuid(arguments.get("check_id"), "check_id"),
            validate_int_range(
                arguments.get("minutes"), "minutes", default=15, minimum=1, maximum=1440
            ),
        )
    )


async def handle_unsnooze_alert_check(arguments: dict[str, Any]) -> list[TextContent]:
    await AdminAlertCoreService.unsnooze(
        get_db(),
        principal().user.username,
        validate_uuid(arguments.get("alert_id"), "alert_id"),
        validate_uuid(arguments.get("check_id"), "check_id"),
    )
    return text_response("Snooze ended")


HANDLERS = {
    "list_alerts": handle_list_alerts,
    "get_alert": handle_get_alert,
    "snooze_alert_check": handle_snooze_alert_check,
    "unsnooze_alert_check": handle_unsnooze_alert_check,
}
