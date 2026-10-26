"""
Admin API check router - checks, addressed by their own id.

Reads take any API token; writes take an editor's or an admin's. A synthetic
check is an admin's alone, which the core service enforces.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiEditor, ApiUser
from app.core.config import settings
from app.core.query_params import AgentIdFilter, CheckTypeFilter
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_check_schema import AdminCheckClone, AdminCheckCreate, CheckPage
from app.schemas.check_schema import CheckResponse, CheckUpdate
from app.services.core.admin_check_core_service import AdminCheckCoreService

router = APIRouter(prefix="/checks", tags=["Admin API: Checks"])

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=CheckPage, name="admin_list_checks")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_checks(
    request: Request,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    agent_id: AgentIdFilter = None,
    check_type: CheckTypeFilter = None,
    enabled_only: bool = False,
    tag: Annotated[str | None, Query(max_length=100)] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    include_internal: bool = False,
) -> CheckPage:
    """One page of checks: filter by agent, type, tag, enabled, or a name/target search."""
    return await AdminCheckCoreService.list_checks(
        db,
        principal.user.role,
        page=page,
        per_page=per_page,
        agent_id=agent_id,
        check_type=check_type,
        enabled_only=enabled_only,
        tag=tag,
        search=search,
        include_internal=include_internal,
    )


@router.get("/{check_id}", response_model=CheckResponse, name="admin_get_check")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_check(
    request: Request,
    check_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> CheckResponse:
    return await AdminCheckCoreService.get_check(db, principal.user.role, check_id)


@router.post(
    "",
    response_model=CheckResponse,
    status_code=status.HTTP_201_CREATED,
    name="admin_create_check",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_create_check(
    request: Request,
    data: AdminCheckCreate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> CheckResponse:
    return await AdminCheckCoreService.create_check(db, principal.user.role, data)


@router.patch("/{check_id}", response_model=CheckResponse, name="admin_update_check")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_update_check(
    request: Request,
    check_id: UUID,
    data: CheckUpdate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> CheckResponse:
    return await AdminCheckCoreService.update_check(
        db, principal.user.role, check_id, data
    )


@router.delete(
    "/{check_id}", status_code=status.HTTP_204_NO_CONTENT, name="admin_delete_check"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_delete_check(
    request: Request,
    check_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    await AdminCheckCoreService.delete_check(db, check_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{check_id}/clone",
    response_model=CheckResponse,
    status_code=status.HTTP_201_CREATED,
    name="admin_clone_check",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_clone_check(
    request: Request,
    check_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    data: AdminCheckClone | None = None,
) -> CheckResponse:
    return await AdminCheckCoreService.clone_check(
        db, principal.user.role, check_id, data or AdminCheckClone()
    )


@router.post(
    "/{check_id}/enable", response_model=CheckResponse, name="admin_enable_check"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_enable_check(
    request: Request,
    check_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> CheckResponse:
    return await AdminCheckCoreService.set_enabled(
        db, principal.user.role, check_id, enabled=True
    )


@router.post(
    "/{check_id}/disable", response_model=CheckResponse, name="admin_disable_check"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_disable_check(
    request: Request,
    check_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> CheckResponse:
    return await AdminCheckCoreService.set_enabled(
        db, principal.user.role, check_id, enabled=False
    )
