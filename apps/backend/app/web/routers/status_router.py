"""
Status router - web UI for status/dashboard view.

Renders HTML pages and HTMX partials. All view-context assembly lives in
StatusViewService.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.query_params import (
    AgentIdFilter,
    CheckTypeFilter,
    HealthStatusFilter,
    PerPageFilter,
)
from app.db import get_db
from app.services.views.status_view_service import StatusViewService
from app.web.deps import CurrentUserWeb
from app.web.templates import templates

logger = get_logger("luxswirl.web.routers.status")

router = APIRouter(tags=["Web UI - Status"])


@router.get("/", response_class=RedirectResponse, include_in_schema=False)
async def root_redirect(request: Request):
    qs = request.url.query
    target = "/dashboard" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=302)


@router.get("/internal/summary", response_class=HTMLResponse, include_in_schema=False)
async def status_summary_partial(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """HTMX partial - summary stats header (polled ~10s)."""
    context = await StatusViewService.build_summary_partial_context(
        db,
        request=request,
        current_user=current_user,
    )
    return templates.TemplateResponse(request, "partials/status_summary.html", context)


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def status_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
    agent_id: AgentIdFilter = None,
    check_type: CheckTypeFilter = None,
    status: HealthStatusFilter = None,
    tags: Annotated[str | None, Query(description="Filter by tags")] = None,
    search: Annotated[str | None, Query(description="Search checks")] = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: PerPageFilter = None,
):
    """Main status dashboard - split-screen layout with inline 15-minute bars."""
    context = await StatusViewService.build_dashboard_context(
        db,
        request=request,
        current_user=current_user,
        agent_id=agent_id,
        check_type=check_type,
        status=status,
        tags=tags,
        search=search,
        page=page,
        per_page=per_page,
        include_minute_bars=True,
        page_title="Status - Monitoring Dashboard",
    )
    return templates.TemplateResponse(request, "pages/status.html", context)
