"""
Notification Logs router - web UI for viewing notification history.

All view-context assembly lives in NotificationLogsViewService.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentUserWeb
from app.core.query_params import AlertIdFilter, NotifStatusFilter, ProviderIdFilter
from app.db import get_db
from app.services.views.notification_logs_view_service import NotificationLogsViewService
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.notification_logs")

router = APIRouter(tags=["Web UI - Notification Logs"])


@router.get("/notification-logs", response_class=HTMLResponse, include_in_schema=False)
async def notification_logs_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
    status: NotifStatusFilter = None,
    alert_id: AlertIdFilter = None,
    notification_provider_id: ProviderIdFilter = None,
    search: Annotated[str | None, Query(description="Search logs")] = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: Annotated[
        int | None, Query(ge=10, le=200, description="Items per page (defaults to setting)")
    ] = None,
):
    """Notification logs page - all notification attempts with pagination."""
    try:
        context = await NotificationLogsViewService.build_logs_page_context(
            db,
            request=request,
            current_user=current_user,
            status=status,
            alert_id=alert_id,
            notification_provider_id=notification_provider_id,
            search=search,
            page=page,
            per_page=per_page,
        )
        return templates.TemplateResponse(request, "pages/notification_logs.html", context)
    except Exception as e:
        logger.error("Error rendering notification logs page", exc_info=True)
        return templates.TemplateResponse(
            request,
            "pages/error.html",
            {
                "current_user": current_user,
                "error": str(e),
                "page_title": "Error",
            },
            status_code=500,
        )
