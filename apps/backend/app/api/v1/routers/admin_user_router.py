"""
Admin API user router - user accounts, addressed by their own id.

Every route takes an admin's token, as the web UI's user pages take an admin.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiAdmin, require_scope
from app.core.config import settings
from app.core.openapi_operations import HandlerNamedRoute
from app.core.query_params import ActiveFilter, UserRoleFilter
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_page_schema import MAX_PAGE
from app.schemas.admin_user_schema import (
    AdminPasswordReset,
    AdminUserCreate,
    AdminUserUpdate,
    UserPage,
)
from app.schemas.user_schema import UserResponse
from app.services.core.admin_user_core_service import AdminUserCoreService

router = APIRouter(
    route_class=HandlerNamedRoute,
    prefix="/users",
    tags=["Admin API: Users"],
    dependencies=[Depends(require_scope("users", admin_writes=True))],
)

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=UserPage, name="api.admin.users.index")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_users(
    request: Request,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    role: UserRoleFilter = None,
    is_active: ActiveFilter = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> UserPage:
    """One page of users; by `role`, `is_active`, or `search` over names."""
    return await AdminUserCoreService.list_users(
        db,
        page=page,
        per_page=per_page,
        role=role,
        is_active=is_active,
        search=search,
    )


@router.get("/{user_id}", response_model=UserResponse, name="api.admin.users.show")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_user(
    request: Request,
    user_id: UUID,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> UserResponse:
    return await AdminUserCoreService.get_user(db, user_id)


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    name="api.admin.users.create",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_create_user(
    request: Request,
    data: AdminUserCreate,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> UserResponse:
    """A new user; the password must meet the policy, and a taken username is
    a 409."""
    return await AdminUserCoreService.create_user(
        db, principal.user.id, principal.user.username, data
    )


@router.patch("/{user_id}", response_model=UserResponse, name="api.admin.users.update")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_update_user(
    request: Request,
    user_id: UUID,
    data: AdminUserUpdate,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> UserResponse:
    """Change the fields sent; a role change ends the user's sessions, and a
    change that leaves no active admin is a 409."""
    return await AdminUserCoreService.update_user(db, principal.user.id, user_id, data)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    name="api.admin.users.delete",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_delete_user(
    request: Request,
    user_id: UUID,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    """Delete the user; not your own account, nor the last active admin."""
    await AdminUserCoreService.delete_user(db, principal.user.id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{user_id}/reset-password",
    response_model=UserResponse,
    name="api.admin.users.reset_password",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
@limiter.limit(
    settings.security.password_reset_rate_limit, key_func=api_token_rate_limit_key
)
async def admin_reset_user_password(
    request: Request,
    user_id: UUID,
    data: AdminPasswordReset,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> UserResponse:
    """Set the user's password; their sessions and API tokens end."""
    return await AdminUserCoreService.reset_password(
        db, principal.user.id, user_id, data
    )


@router.post(
    "/{user_id}/unlock", response_model=UserResponse, name="api.admin.users.unlock"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_unlock_user(
    request: Request,
    user_id: UUID,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> UserResponse:
    """Clear the user's failed sign-ins and lock."""
    return await AdminUserCoreService.unlock_user(db, principal.user.id, user_id)
