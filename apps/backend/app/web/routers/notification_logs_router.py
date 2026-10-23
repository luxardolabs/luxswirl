"""
Notification Logs router - web UI for viewing notification history.

All view-context assembly lives in NotificationLogsViewService.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.query_params import (
    AlertIdFilter,
    NotifStatusFilter,
    PerPageFilter,
    ProviderIdFilter,
)
from app.db import get_db
from app.services.views.notification_logs_view_service import (
    NotificationLogsViewService,
)
from app.shared.logger import get_logger
from app.web.deps import CurrentUserWeb
from app.web.templates import templates

logger = get_logger("luxswirl.web.routers.notification_logs")

router = APIRouter(tags=["Web UI - Notification Logs"])


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/notification-logs",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="notification_logs_page",
)
async def notification_logs_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: CurrentUserWeb,
    status: NotifStatusFilter = None,
    alert_id: AlertIdFilter = None,
    notification_provider_id: ProviderIdFilter = None,
    search: Annotated[str | None, Query(description="Search logs")] = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: PerPageFilter = None,
) -> HTMLResponse:
    """Notification logs page - all notification attempts with pagination."""
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
