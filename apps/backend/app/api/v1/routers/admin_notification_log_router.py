"""
Admin API notification log router - every notification attempt and its outcome.

Any API token reads it, as anyone signed in reads the web UI's log page.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiUser
from app.core.config import settings
from app.core.query_params import AlertIdFilter, NotifStatusFilter, ProviderIdFilter
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_notification_schema import NotificationLogPage
from app.schemas.admin_page_schema import MAX_PAGE
from app.services.core.admin_notification_core_service import (
    AdminNotificationCoreService,
)

router = APIRouter(prefix="/notification-logs", tags=["Admin API: Notification logs"])

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=NotificationLogPage, name="admin_list_notification_logs")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_notification_logs(
    request: Request,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    status: NotifStatusFilter = None,
    alert_id: AlertIdFilter = None,
    notification_provider_id: ProviderIdFilter = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> NotificationLogPage:
    """One page of notification attempts, newest first, filtered by `status`,
    `alert_id`, `notification_provider_id` or a `search` of alert, provider and check names."""
    return await AdminNotificationCoreService.list_logs(
        db,
        page=page,
        per_page=per_page,
        status=status,
        alert_id=alert_id,
        notification_provider_id=notification_provider_id,
        search=search,
    )
