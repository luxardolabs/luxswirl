"""
Check router - web UI for check details.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentUserWeb
from app.db import get_db
from app.services.views.check_detail_view_service import CheckDetailViewService
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.check")

router = APIRouter(tags=["Web UI - Checks"])


@router.get("/check/{check_id}", response_class=HTMLResponse, include_in_schema=False)
async def check_detail_panel(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID")],
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    hours: Annotated[
        int | None,
        Query(ge=1, le=168, description="Hours of history to show (defaults to setting)"),
    ] = None,
):
    """
    Check detail panel for split-screen layout - opened when clicking a check row on dashboard.
    """
    try:
        # Get default chart time range from settings if not specified
        if hours is None:
            time_range_str = await CheckDetailViewService.get_setting(
                db, "general.default_chart_time_range", "4h"
            )
            hours = CheckDetailViewService.parse_time_range_to_hours(time_range_str)

        detail = await CheckDetailViewService.get_check_detail(db, check_id, hours=hours)

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
            "panels/checks/check_detail_panel.html",
            {
                "current_user": current_user,
                "check": detail["check"],
                "agent": detail["agent"],
                "history": detail["history"],
                "recent_results": detail.get("recent_results", []),
                "minute_bars": detail["minute_bars"],
                "stats": detail["stats"],
                "hours": detail["hours"],
                "check_id": check_id,
                "chart_data": detail["chart_data"],
                "artifacts": detail.get("artifacts", []),
            },
        )
    except Exception as e:
        logger.error("Error rendering check detail", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {
                "current_user": current_user,
                "error": str(e),
            },
            status_code=500,
        )


@router.get("/checks/detail/empty", response_class=HTMLResponse, include_in_schema=False)
async def check_detail_empty(
    request: Request,
    current_user: CurrentUserWeb,
):
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
