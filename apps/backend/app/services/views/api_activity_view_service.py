"""
API activity view service - template context for the audited token calls: the
admin page over every user's MCP tool calls and admin API requests, and each
user's own in My Account.

Ported from LuxWX's `MCPAdminViewService` (`clamp_days`, the outcome filter, the
call detail), without tenants and stations, and with a second surface: the admin
API's own call audit. One service serves both screens, so they cannot disagree on
what a window or an outcome means; My Account always passes its own user.
"""

from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.models.enum_model import AuditOutcome, AuditSurface
from app.schemas.api_activity_schema import (
    ActivityCaller,
    ActivityFilters,
    ActivityOption,
    ApiCallRow,
    McpCallRow,
)
from app.schemas.auth_schema import AuthedUser
from app.services.core.api_audit_core_service import ApiAuditCoreService
from app.services.core.mcp_audit_core_service import MCPAuditCoreService

# The windows the screens offer. Every list is time-bounded because each table
# grows a row per call; a hand-edited ?days=3650 must not become a full scan.
ALLOWED_DAYS: tuple[int, ...] = (1, 7, 30, 90)
DEFAULT_DAYS = 7
# The most rows the admin page lists; narrow the filters to see further back.
PAGE_ROWS = 200
# The most rows My Account lists.
OWN_ROWS = 25

# How each outcome reads, and the badge colour it wears.
OUTCOME_LABELS: dict[AuditOutcome, str] = {
    AuditOutcome.SUCCESS: "Succeeded",
    AuditOutcome.ERROR: "Failed",
    AuditOutcome.DENIED: "Denied",
}
OUTCOME_VARIANTS: dict[AuditOutcome, str] = {
    AuditOutcome.SUCCESS: "success",
    AuditOutcome.ERROR: "danger",
    AuditOutcome.DENIED: "warning",
}
SURFACE_LABELS: dict[AuditSurface, str] = {
    AuditSurface.MCP: "MCP tool calls",
    AuditSurface.API: "Admin API requests",
}


def clamp_days(days: int) -> int:
    """Only honor the windows the screen offers."""
    return days if days in ALLOWED_DAYS else DEFAULT_DAYS


def _user_options(callers: list[ActivityCaller]) -> list[ActivityOption]:
    names: dict[UUID, str] = {}
    for caller in callers:
        if caller.user_id is not None:
            names[caller.user_id] = caller.username or "(deleted user)"
    return sorted(
        (ActivityOption(value=str(k), label=v) for k, v in names.items()),
        key=lambda option: option.label.lower(),
    )


def _token_options(callers: list[ActivityCaller]) -> list[ActivityOption]:
    names: dict[UUID, str] = {}
    for caller in callers:
        if caller.api_token_id is not None:
            token = caller.token_name or "(deleted token)"
            names[caller.api_token_id] = (
                f"{token} ({caller.username or 'deleted user'})"
            )
    return sorted(
        (ActivityOption(value=str(k), label=v) for k, v in names.items()),
        key=lambda option: option.label.lower(),
    )


def _shown(call: McpCallRow | ApiCallRow, **extra: str) -> dict[str, Any]:
    """A row as the templates render it: the call, and its outcome in words."""
    return {
        "call": call,
        "outcome_label": OUTCOME_LABELS[call.outcome],
        "outcome_variant": OUTCOME_VARIANTS[call.outcome],
        **extra,
    }


class ApiActivityViewService:
    """Builds the API activity page, its call detail, and My Account's section."""

    @staticmethod
    async def build_page_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        *,
        surface: AuditSurface | None,
        days: int,
        tool: str | None,
        route: str | None,
        api_token_id: UUID | None,
        user_id: UUID | None,
        outcome: AuditOutcome | None,
    ) -> dict[str, Any]:
        """The admin page: every user's calls on one surface, filtered."""
        surface = surface or AuditSurface.MCP
        days = clamp_days(days)
        filters = ActivityFilters(
            days=days,
            user_id=user_id,
            api_token_id=api_token_id,
            tool_name=(tool or None) if surface is AuditSurface.MCP else None,
            route=(route or None) if surface is AuditSurface.API else None,
            outcome=outcome,
        )
        page_url = str(request.app.url_path_for("api_activity_page"))
        rows: list[dict[str, Any]]
        if surface is AuditSurface.MCP:
            callers = await MCPAuditCoreService.callers(db, days)
            operations = await MCPAuditCoreService.tool_names(db, days, None)
            rows = [
                _shown(
                    call,
                    detail_url=str(
                        request.app.url_path_for(
                            "api_activity_mcp_call", call_id=str(call.id)
                        )
                    ),
                )
                for call in await MCPAuditCoreService.list_calls(db, filters, PAGE_ROWS)
            ]
        else:
            callers = await ApiAuditCoreService.callers(db, days)
            operations = await ApiAuditCoreService.routes(db, days, None)
            rows = [
                _shown(call)
                for call in await ApiAuditCoreService.list_calls(db, filters, PAGE_ROWS)
            ]
        return {
            "request": request,
            "current_user": current_user,
            "page_title": "API Activity",
            "list_url": page_url,
            "surface": surface.value,
            "surface_tabs": [
                {
                    "label": label,
                    "url": f"{page_url}?surface={s.value}&days={days}",
                    "active": s is surface,
                }
                for s, label in SURFACE_LABELS.items()
            ],
            "rows": rows,
            "row_limit": PAGE_ROWS,
            "row_count": len(rows),
            "truncated": len(rows) >= PAGE_ROWS,
            "filters": {
                "days": days,
                "tool": filters.tool_name or "",
                "route": filters.route or "",
                "api_token_id": str(api_token_id) if api_token_id else "",
                "user_id": str(user_id) if user_id else "",
                "outcome": outcome.value if outcome else "",
            },
            "filter_options": {
                "days": list(ALLOWED_DAYS),
                "operations": [ActivityOption(value=o, label=o) for o in operations],
                "users": _user_options(callers),
                "api_tokens": _token_options(callers),
                "outcomes": [
                    ActivityOption(value=o.value, label=label)
                    for o, label in OUTCOME_LABELS.items()
                ],
            },
        }

    @staticmethod
    async def build_mcp_call_context(
        db: AsyncSession, request: Request, current_user: AuthedUser, call_id: UUID
    ) -> dict[str, Any]:
        """One MCP tool call in full, its arguments masked.

        Raises:
            NotFoundException: There is no such call.
        """
        call = await MCPAuditCoreService.get_call(db, call_id)
        if call is None:
            raise NotFoundException(f"Tool call not found: {call_id}")
        return {
            "request": request,
            "current_user": current_user,
            **_shown(call),
        }

    @staticmethod
    async def build_own_activity_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        *,
        surface: AuditSurface | None,
        days: int,
    ) -> dict[str, Any]:
        """My Account: the signed-in user's own calls on one surface, never anyone
        else's (the filter is always their own id)."""
        surface = surface or AuditSurface.MCP
        days = clamp_days(days)
        filters = ActivityFilters(days=days, user_id=current_user.id)
        own_url = str(request.app.url_path_for("profile_activity"))
        calls: list[McpCallRow] | list[ApiCallRow]
        if surface is AuditSurface.MCP:
            calls = await MCPAuditCoreService.list_calls(db, filters, OWN_ROWS)
        else:
            calls = await ApiAuditCoreService.list_calls(db, filters, OWN_ROWS)
        return {
            "request": request,
            "current_user": current_user,
            "surface": surface.value,
            "days": days,
            "rows": [_shown(call) for call in calls],
            "row_limit": OWN_ROWS,
            "surface_tabs": [
                {
                    "label": label,
                    "url": f"{own_url}?surface={s.value}&days={days}",
                    "active": s is surface,
                }
                for s, label in SURFACE_LABELS.items()
            ],
            "day_tabs": [
                {
                    "label": f"{d}d",
                    "url": f"{own_url}?surface={surface.value}&days={d}",
                    "active": d == days,
                }
                for d in ALLOWED_DAYS
            ],
        }
