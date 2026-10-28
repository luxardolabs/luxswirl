"""
API activity router - the audited MCP tool calls and admin API requests.

The admin page lists every user's calls (admin only); My Account's section lists
the signed-in user's own. Ported from LuxWX's admin MCP usage routes, without
tenants and stations.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.query_params import (
    ApiTokenIdFilter,
    AuditOutcomeFilter,
    AuditSurfaceFilter,
    UserIdFilter,
)
from app.db import get_db
from app.services.views.api_activity_view_service import (
    DEFAULT_DAYS,
    ApiActivityViewService,
)
from app.web.deps import AdminUserWeb, CurrentUserWeb
from app.web.templates import templates

router = APIRouter(tags=["Web UI - API Activity"], include_in_schema=False)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/settings/api-activity",
    response_class=HTMLResponse,
    name="api_activity_page",
)
async def api_activity_page(
    request: Request,
    current_user: AdminUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    surface: AuditSurfaceFilter = None,
    days: int = DEFAULT_DAYS,
    tool: str | None = None,
    route: str | None = None,
    api_token_id: ApiTokenIdFilter = None,
    user_id: UserIdFilter = None,
    outcome: AuditOutcomeFilter = None,
) -> HTMLResponse:
    """Every user's MCP tool calls or admin API requests (admin only)."""
    context = await ApiActivityViewService.build_page_context(
        db,
        request,
        current_user,
        surface=surface,
        days=days,
        tool=tool,
        route=route,
        api_token_id=api_token_id,
        user_id=user_id,
        outcome=outcome,
    )
    return templates.TemplateResponse(
        request, "pages/settings/api_activity.html", context
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/settings/api-activity/mcp/{call_id}",
    response_class=HTMLResponse,
    name="api_activity_mcp_call",
)
async def api_activity_mcp_call(
    request: Request,
    call_id: UUID,
    current_user: AdminUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> HTMLResponse:
    """One MCP tool call in full: its masked arguments and its error (admin only)."""
    context = await ApiActivityViewService.build_mcp_call_context(
        db, request, current_user, call_id
    )
    return templates.TemplateResponse(
        request, "partials/settings/api_activity_call_panel.html", context
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/profile/activity",
    response_class=HTMLResponse,
    name="profile_activity",
)
async def profile_activity(
    request: Request,
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    surface: AuditSurfaceFilter = None,
    days: int = DEFAULT_DAYS,
) -> HTMLResponse:
    """My Account: the signed-in user's own MCP tool calls or admin API requests."""
    context = await ApiActivityViewService.build_own_activity_context(
        db, request, current_user, surface=surface, days=days
    )
    return templates.TemplateResponse(
        request, "partials/profile/api_activity.html", context
    )
