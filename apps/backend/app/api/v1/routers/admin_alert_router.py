"""
Admin API alert router - alert rules, addressed by their own id.

Reads take any API token; every change takes an editor's, as in the web UI.
Snoozing an alert for one check takes any token, as any signed-in user may
snooze one in the web UI.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiEditor, ApiUser
from app.core.config import settings
from app.core.query_params import EnabledFilter, GlobalFilter
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_alert_schema import (
    AdminAlertChecks,
    AdminAlertCreate,
    AdminAlertDetail,
    AdminAlertProviders,
    AdminAlertSnooze,
    AdminAlertUpdate,
    AlertMappingChange,
    AlertPage,
)
from app.schemas.admin_page_schema import MAX_PAGE
from app.schemas.alert_schema import AlertCheckSnooze
from app.services.core.admin_alert_core_service import AdminAlertCoreService

router = APIRouter(prefix="/alerts", tags=["Admin API: Alerts"])

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=AlertPage, name="admin_list_alerts")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_alerts(
    request: Request,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    is_enabled: EnabledFilter = None,
    is_global: GlobalFilter = None,
) -> AlertPage:
    """One page of alerts, filtered by `is_enabled` and `is_global`."""
    return await AdminAlertCoreService.list_alerts(
        db, page=page, per_page=per_page, is_enabled=is_enabled, is_global=is_global
    )


@router.get("/{alert_id}", response_model=AdminAlertDetail, name="admin_get_alert")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_alert(
    request: Request,
    alert_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminAlertDetail:
    """The alert, with the providers it notifies and the checks it watches."""
    return await AdminAlertCoreService.get_alert(db, alert_id)


@router.post(
    "",
    response_model=AdminAlertDetail,
    status_code=status.HTTP_201_CREATED,
    name="admin_create_alert",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_create_alert(
    request: Request,
    data: AdminAlertCreate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminAlertDetail:
    return await AdminAlertCoreService.create_alert(db, principal.user.role, data)


@router.patch("/{alert_id}", response_model=AdminAlertDetail, name="admin_update_alert")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_update_alert(
    request: Request,
    alert_id: UUID,
    data: AdminAlertUpdate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminAlertDetail:
    """Change the fields sent; a field left out is unchanged."""
    return await AdminAlertCoreService.update_alert(db, alert_id, data)


@router.delete(
    "/{alert_id}", status_code=status.HTTP_204_NO_CONTENT, name="admin_delete_alert"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_delete_alert(
    request: Request,
    alert_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    await AdminAlertCoreService.delete_alert(db, alert_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{alert_id}/enable", response_model=AdminAlertDetail, name="admin_enable_alert"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_enable_alert(
    request: Request,
    alert_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminAlertDetail:
    return await AdminAlertCoreService.set_enabled(db, alert_id, enabled=True)


@router.post(
    "/{alert_id}/disable", response_model=AdminAlertDetail, name="admin_disable_alert"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_disable_alert(
    request: Request,
    alert_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminAlertDetail:
    return await AdminAlertCoreService.set_enabled(db, alert_id, enabled=False)


@router.put(
    "/{alert_id}/providers",
    response_model=AlertMappingChange,
    name="admin_set_alert_providers",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_set_alert_providers(
    request: Request,
    alert_id: UUID,
    data: AdminAlertProviders,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AlertMappingChange:
    """Make exactly these providers the ones the alert notifies."""
    return await AdminAlertCoreService.set_providers(db, alert_id, data)


@router.put(
    "/{alert_id}/checks",
    response_model=AlertMappingChange,
    name="admin_set_alert_checks",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_set_alert_checks(
    request: Request,
    alert_id: UUID,
    data: AdminAlertChecks,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AlertMappingChange:
    """Make exactly these checks the ones a non-global alert watches."""
    return await AdminAlertCoreService.set_checks(
        db, principal.user.role, alert_id, data
    )


@router.post(
    "/{alert_id}/checks/{check_id}/snooze",
    response_model=AlertCheckSnooze,
    name="admin_snooze_alert_check",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_snooze_alert_check(
    request: Request,
    alert_id: UUID,
    check_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    data: AdminAlertSnooze | None = None,
) -> AlertCheckSnooze:
    """Hold back this alert's notifications for this check (15 minutes unless
    `minutes` says otherwise; a running snooze is extended, to at most a day
    from now)."""
    return await AdminAlertCoreService.snooze(
        db,
        principal.user.username,
        alert_id,
        check_id,
        (data or AdminAlertSnooze()).minutes,
    )


@router.delete(
    "/{alert_id}/checks/{check_id}/snooze",
    status_code=status.HTTP_204_NO_CONTENT,
    name="admin_unsnooze_alert_check",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_unsnooze_alert_check(
    request: Request,
    alert_id: UUID,
    check_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    """Resume this alert's notifications for this check now."""
    await AdminAlertCoreService.unsnooze(
        db, principal.user.username, alert_id, check_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
