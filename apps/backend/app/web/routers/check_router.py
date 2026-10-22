"""
Check router - web UI for check details.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.query_params import HistoryHoursFilter
from app.db import get_db
from app.services.views.check_detail_view_service import CheckDetailViewService
from app.web.deps import CurrentUserWeb
from app.web.templates import templates

logger = get_logger("luxswirl.web.routers.check")

router = APIRouter(tags=["Web UI - Checks"])


@router.get(
    "/check/{check_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="check_detail_panel",
)
async def check_detail_panel(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID")],
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    hours: HistoryHoursFilter = None,
) -> HTMLResponse:
    """
    Check detail panel for split-screen layout - opened when clicking a check row on dashboard.
    """
    if hours is None:
        hours = await CheckDetailViewService.resolve_default_chart_hours(db)

    detail = await CheckDetailViewService.get_check_detail(
        db, request, check_id, hours=hours
    )

    if not detail:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {
                "current_user": current_user,
                "error": f"Check {check_id} not found",
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        request,
        "partials/panels/checks/check_detail_panel.html",
        # Hand the whole view-model through. Re-listing keys here silently dropped
        # parent_check / parent_latest_result / dependent_count, so the panel's
        # dependency section never rendered.
        {"current_user": current_user, "check_id": check_id, **detail},
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/checks/detail/empty",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="check_detail_empty",
)
async def check_detail_empty(
    request: Request,
    current_user: CurrentUserWeb,
) -> HTMLResponse:
    """
    Return empty state for check detail panel.

    Used when deleting a check to restore the default empty state.
    """
    return templates.TemplateResponse(
        request,
        "partials/checks/check_detail_empty.html",
        {
            "current_user": current_user,
        },
    )
