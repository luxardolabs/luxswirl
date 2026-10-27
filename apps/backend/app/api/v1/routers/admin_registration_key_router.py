"""
Admin API registration key router - the keys agents register with.

Every route takes an admin's token, as the web UI's key pages take an admin.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiAdmin, require_scope
from app.core.config import settings
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_page_schema import MAX_PAGE
from app.schemas.admin_registration_key_schema import (
    AdminRegistrationKeyCreate,
    AdminRegistrationKeyRevoke,
    RegistrationKeyCreated,
    RegistrationKeyPage,
)
from app.schemas.registration_key_schema import RegistrationKeyResponse
from app.services.core.admin_registration_key_core_service import (
    AdminRegistrationKeyCoreService,
)

router = APIRouter(
    prefix="/registration-keys",
    tags=["Admin API: Registration keys"],
    dependencies=[Depends(require_scope("registration_keys", admin_writes=True))],
)

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=RegistrationKeyPage, name="admin_list_registration_keys")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_registration_keys(
    request: Request,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    include_revoked: bool = False,
) -> RegistrationKeyPage:
    """One page of keys, newest first; revoked ones only with `include_revoked`."""
    return await AdminRegistrationKeyCoreService.list_keys(
        db, page=page, per_page=per_page, include_revoked=include_revoked
    )


@router.get(
    "/{key_id}",
    response_model=RegistrationKeyResponse,
    name="admin_get_registration_key",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_registration_key(
    request: Request,
    key_id: UUID,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> RegistrationKeyResponse:
    return await AdminRegistrationKeyCoreService.get_key(db, key_id)


@router.post(
    "",
    response_model=RegistrationKeyCreated,
    status_code=status.HTTP_201_CREATED,
    name="admin_create_registration_key",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_create_registration_key(
    request: Request,
    data: AdminRegistrationKeyCreate,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> RegistrationKeyCreated:
    """A new key; its plaintext `key` is in this answer and nowhere again."""
    return await AdminRegistrationKeyCoreService.create_key(
        db, principal.user.username, data
    )


@router.post(
    "/{key_id}/revoke",
    response_model=RegistrationKeyResponse,
    name="admin_revoke_registration_key",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_revoke_registration_key(
    request: Request,
    key_id: UUID,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    data: AdminRegistrationKeyRevoke | None = None,
) -> RegistrationKeyResponse:
    """Revoke the key: no agent registers or recovers its key with it again."""
    return await AdminRegistrationKeyCoreService.revoke_key(
        db, principal.user.username, key_id, data or AdminRegistrationKeyRevoke()
    )
