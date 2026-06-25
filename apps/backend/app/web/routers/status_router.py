"""
Status router - web UI for status/dashboard view.

Renders HTML pages and HTMX partials. All view-context assembly lives in
StatusViewService.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentUserWeb
from app.core.query_params import CheckTypeFilter, HealthStatusFilter
from app.db import get_db
from app.services.views.status_view_service import StatusViewService
from app.web.routers._render import error_page, error_partial
from app.web.templates_config import templates

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
    try:
        context = await StatusViewService.build_summary_partial_context(
            db,
            request=request,
            current_user=current_user,
        )
        return templates.TemplateResponse(request, "partials/status_summary.html", context)
    except Exception as e:
        logger.error("Error rendering status summary partial", exc_info=True)
        return error_partial(
            request, current_user, str(e), status_code=500, template="partials/error.html"
        )


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def status_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
    agent_id: Annotated[UUID | None, Query(description="Filter by agent")] = None,
    check_type: CheckTypeFilter = None,
    status: HealthStatusFilter = None,
    tags: Annotated[str | None, Query(description="Filter by tags")] = None,
    search: Annotated[str | None, Query(description="Search checks")] = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: Annotated[
        int | None, Query(ge=10, le=200, description="Items per page (defaults to setting)")
    ] = None,
):
    """Main status dashboard - split-screen layout with inline 15-minute bars."""
    try:
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
    except Exception as e:
        logger.error("Error rendering status page", exc_info=True)
        return error_page(request, current_user, str(e))
