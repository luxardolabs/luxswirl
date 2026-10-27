"""
Admin API settings router - runtime settings, addressed by key.

Every route takes an admin's token, as the web UI's settings pages take an
admin.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiAdmin, require_scope
from app.core.config import settings
from app.core.query_params import SettingCategoryFilter
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_setting_schema import (
    AdminSetting,
    AdminSettingValue,
    MetricsToken,
)
from app.services.core.admin_setting_core_service import AdminSettingCoreService

router = APIRouter(
    prefix="/settings",
    tags=["Admin API: Settings"],
    dependencies=[Depends(require_scope("settings", admin_writes=True))],
)

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=list[AdminSetting], name="admin_list_settings")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_settings(
    request: Request,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    category: SettingCategoryFilter = None,
) -> list[AdminSetting]:
    """Every setting by key, or one `category`'s."""
    return await AdminSettingCoreService.list_settings(db, category)


@router.post(
    "/metrics-token",
    response_model=MetricsToken,
    status_code=status.HTTP_201_CREATED,
    name="admin_generate_metrics_token",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_generate_metrics_token(
    request: Request,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> MetricsToken:
    """A new bearer token for `/metrics`, in this answer and nowhere again; the
    old one stops working now."""
    return await AdminSettingCoreService.generate_metrics_token(db)


@router.get("/{key}", response_model=AdminSetting, name="admin_get_setting")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_setting(
    request: Request,
    key: str,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminSetting:
    return await AdminSettingCoreService.get_setting(db, key)


@router.put("/{key}", response_model=AdminSetting, name="admin_set_setting")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_set_setting(
    request: Request,
    key: str,
    data: AdminSettingValue,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminSetting:
    """Set the value; it must be of the setting's type and meet its rules."""
    return await AdminSettingCoreService.set_setting(db, key, data)


@router.post("/{key}/reset", response_model=AdminSetting, name="admin_reset_setting")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_reset_setting(
    request: Request,
    key: str,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminSetting:
    """Set the value back to its default."""
    return await AdminSettingCoreService.reset_setting(db, key)
