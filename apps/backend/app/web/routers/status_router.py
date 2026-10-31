"""
Status router - web UI for status/dashboard view.

Renders HTML pages and HTMX partials. All view-context assembly lives in
StatusViewService.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.query_params import (
    AgentIdFilter,
    CheckTypeFilter,
    HealthStatusFilter,
    PerPageFilter,
)
from app.core.safe_redirect import safe_redirect_target
from app.db import get_db
from app.services.views.status_view_service import StatusViewService
from app.shared.logger import get_logger
from app.web.deps import CurrentUserWeb
from app.web.templates import templates

logger = get_logger("luxswirl.web.routers.status")

router = APIRouter(tags=["Web UI - Status"])


@router.get(
    "/", response_class=RedirectResponse, include_in_schema=False, name="home.redirect"
)
async def root_redirect(request: Request) -> Response:
    qs = request.url.query
    dashboard = request.app.url_path_for("dashboard.index")
    target = dashboard + (f"?{qs}" if qs else "")
    return RedirectResponse(
        url=safe_redirect_target(target, default=dashboard), status_code=302
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/internal/summary",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="dashboard.summary",
)
async def status_summary_partial(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: CurrentUserWeb,
) -> HTMLResponse:
    """HTMX partial - summary stats header (polled ~10s)."""
    context = await StatusViewService.build_summary_partial_context(
        db,
        request=request,
        current_user=current_user,
    )
    return templates.TemplateResponse(request, "partials/status_summary.html", context)


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="dashboard.index",
)
async def status_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: CurrentUserWeb,
    agent_id: AgentIdFilter = None,
    check_type: CheckTypeFilter = None,
    status: HealthStatusFilter = None,
    tags: Annotated[str | None, Query(description="Filter by tags")] = None,
    search: Annotated[str | None, Query(description="Search checks")] = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: PerPageFilter = None,
) -> HTMLResponse:
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
