"""
Admin API notification provider router - where alerts send notifications.

Reads take any API token, a viewer's without the providers' configs; every
change takes an editor's, as in the web UI.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiEditor, ApiUser, require_scope
from app.core.config import settings
from app.core.query_params import ProviderTypeFilter
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_notification_schema import (
    AdminProvider,
    AdminProviderCreate,
    AdminProviderUpdate,
    ProviderPage,
    ProviderTestResult,
    ProviderTypeInfo,
)
from app.schemas.admin_page_schema import MAX_PAGE
from app.services.core.admin_notification_core_service import (
    AdminNotificationCoreService,
)

router = APIRouter(
    prefix="/notification-providers",
    tags=["Admin API: Notification providers"],
    dependencies=[Depends(require_scope("notifications"))],
)

LIMIT = settings.security.admin_api_rate_limit


@router.get(
    "", response_model=ProviderPage, name="api.admin.notification_providers.index"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_providers(
    request: Request,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    provider_type: ProviderTypeFilter = None,
) -> ProviderPage:
    """One page of providers; a viewer's token reads them without `config`."""
    return await AdminNotificationCoreService.list_providers(
        db,
        principal.user.role,
        page=page,
        per_page=per_page,
        provider_type=provider_type.value if provider_type else None,
    )


@router.get(
    "/types",
    response_model=list[ProviderTypeInfo],
    name="api.admin.notification_provider_types.index",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_provider_types(
    request: Request, principal: ApiUser
) -> list[ProviderTypeInfo]:
    """Every provider type, and the config fields it takes."""
    return AdminNotificationCoreService.provider_types()


@router.get(
    "/{provider_id}",
    response_model=AdminProvider,
    name="api.admin.notification_providers.show",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_provider(
    request: Request,
    provider_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminProvider:
    return await AdminNotificationCoreService.get_provider(
        db, principal.user.role, provider_id
    )


@router.post(
    "",
    response_model=AdminProvider,
    status_code=status.HTTP_201_CREATED,
    name="api.admin.notification_providers.create",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_create_provider(
    request: Request,
    data: AdminProviderCreate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminProvider:
    return await AdminNotificationCoreService.create_provider(
        db, principal.user.role, data
    )


@router.patch(
    "/{provider_id}",
    response_model=AdminProvider,
    name="api.admin.notification_providers.update",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_update_provider(
    request: Request,
    provider_id: UUID,
    data: AdminProviderUpdate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminProvider:
    """Change the fields sent; a `config` sent replaces the stored one whole."""
    return await AdminNotificationCoreService.update_provider(
        db, principal.user.role, provider_id, data
    )


@router.delete(
    "/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    name="api.admin.notification_providers.delete",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_delete_provider(
    request: Request,
    provider_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    await AdminNotificationCoreService.delete_provider(db, provider_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{provider_id}/enable",
    response_model=AdminProvider,
    name="api.admin.notification_providers.enable",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_enable_provider(
    request: Request,
    provider_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminProvider:
    return await AdminNotificationCoreService.set_enabled(
        db, principal.user.role, provider_id, enabled=True
    )


@router.post(
    "/{provider_id}/disable",
    response_model=AdminProvider,
    name="api.admin.notification_providers.disable",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_disable_provider(
    request: Request,
    provider_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminProvider:
    return await AdminNotificationCoreService.set_enabled(
        db, principal.user.role, provider_id, enabled=False
    )


@router.post(
    "/{provider_id}/test",
    response_model=ProviderTestResult,
    name="api.admin.notification_providers.test",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_test_provider(
    request: Request,
    provider_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> ProviderTestResult:
    """Send the provider a test notification now."""
    return await AdminNotificationCoreService.test_provider(db, provider_id)
