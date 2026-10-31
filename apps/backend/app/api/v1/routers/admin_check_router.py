"""
Admin API check router - checks, addressed by their own id.

Reads take any API token; writes take an editor's or an admin's. A synthetic
check is an admin's alone, which the core service enforces.

The bulk routes are declared before the `/{check_id}/...` ones, which would
otherwise take `bulk` as a check id.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiEditor, ApiUser, ScopedBulkCreate, require_scope
from app.core.config import settings
from app.core.openapi_operations import HandlerNamedRoute
from app.core.query_params import AgentIdFilter, CheckTypeFilter, HealthStatusFilter
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_check_schema import (
    AdminBulkChecks,
    AdminBulkModify,
    AdminCheckClone,
    AdminCheckCreate,
    AdminCheckDependents,
    AdminCheckUpdate,
    BulkCheckResult,
    CheckPage,
    DependentsChange,
)
from app.schemas.admin_page_schema import MAX_PAGE
from app.schemas.check_schema import CheckResponse
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.services.core.admin_check_core_service import AdminCheckCoreService
from app.services.core.check_transfer_core_service import CheckTransferCoreService

router = APIRouter(
    route_class=HandlerNamedRoute,
    prefix="/checks",
    tags=["Admin API: Checks"],
    dependencies=[Depends(require_scope("checks"))],
)

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=CheckPage, name="api.admin.checks.index")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_checks(
    request: Request,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    agent_id: AgentIdFilter = None,
    check_type: CheckTypeFilter = None,
    enabled_only: bool = False,
    tag: Annotated[str | None, Query(max_length=100)] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    status: HealthStatusFilter = None,
) -> CheckPage:
    """One page of checks, each with its health: filter by agent, type, tag,
    enabled, health (up, down, unknown), or a name/target search."""
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
        status=status,
    )


@router.post(
    "/bulk/enable", response_model=BulkCheckResult, name="api.admin.checks.bulk_enable"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_bulk_enable_checks(
    request: Request,
    data: AdminBulkChecks,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> BulkCheckResult:
    """Enable every check named, now."""
    return await AdminCheckCoreService.bulk_set_enabled(
        db, principal.user.role, data, enabled=True
    )


@router.post(
    "/bulk/disable",
    response_model=BulkCheckResult,
    name="api.admin.checks.bulk_disable",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_bulk_disable_checks(
    request: Request,
    data: AdminBulkChecks,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> BulkCheckResult:
    """Disable every check named, now."""
    return await AdminCheckCoreService.bulk_set_enabled(
        db, principal.user.role, data, enabled=False
    )


@router.post(
    "/bulk/delete",
    response_model=MaintenanceJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    name="api.admin.checks.bulk_delete",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_bulk_delete_checks(
    request: Request,
    data: AdminBulkChecks,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> MaintenanceJobResponse:
    """Delete every check named, and its results, in the background.

    Poll `/api/v1/admin/maintenance-jobs/{id}` for the outcome.
    """
    return await AdminCheckCoreService.enqueue_bulk_delete(
        db, principal.user.role, principal.user.id, data
    )


@router.post(
    "/bulk/modify",
    response_model=MaintenanceJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    name="api.admin.checks.bulk_modify",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_bulk_modify_checks(
    request: Request,
    data: AdminBulkModify,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> MaintenanceJobResponse:
    """Change, move to another agent, or assign an alert to every check named,
    in the background. Poll `/api/v1/admin/maintenance-jobs/{id}` for the outcome.
    """
    return await AdminCheckCoreService.enqueue_bulk_modify(
        db, principal.user.role, principal.user.id, data
    )


@router.post(
    "/bulk/create",
    response_model=MaintenanceJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    name="api.admin.checks.bulk_create",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_bulk_create_checks(
    request: Request,
    data: ScopedBulkCreate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> MaintenanceJobResponse:
    """Create one check per URL on an agent, in the background. Poll
    `/api/v1/admin/maintenance-jobs/{id}` for the outcome.
    """
    return await CheckTransferCoreService.enqueue_bulk_create(
        db, principal.user.id, data.agent_id, data.items, data.alert_ids
    )


@router.get("/{check_id}", response_model=CheckResponse, name="api.admin.checks.show")
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
    name="api.admin.checks.create",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_create_check(
    request: Request,
    data: AdminCheckCreate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> CheckResponse:
    return await AdminCheckCoreService.create_check(db, principal.user.role, data)


@router.patch(
    "/{check_id}", response_model=CheckResponse, name="api.admin.checks.update"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_update_check(
    request: Request,
    check_id: UUID,
    data: AdminCheckUpdate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> CheckResponse:
    return await AdminCheckCoreService.update_check(
        db, principal.user.role, check_id, data
    )


@router.delete(
    "/{check_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    name="api.admin.checks.delete",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_delete_check(
    request: Request,
    check_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    await AdminCheckCoreService.delete_check(db, principal.user.role, check_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{check_id}/clone",
    response_model=CheckResponse,
    status_code=status.HTTP_201_CREATED,
    name="api.admin.checks.clone",
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
    "/{check_id}/enable", response_model=CheckResponse, name="api.admin.checks.enable"
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
    "/{check_id}/disable", response_model=CheckResponse, name="api.admin.checks.disable"
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


@router.get(
    "/{check_id}/dependents",
    response_model=list[CheckResponse],
    name="api.admin.check_dependents.index",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_check_dependents(
    request: Request,
    check_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> list[CheckResponse]:
    """The checks that depend on this one."""
    return await AdminCheckCoreService.list_dependents(
        db, principal.user.role, check_id
    )


@router.put(
    "/{check_id}/dependents",
    response_model=DependentsChange,
    name="api.admin.check_dependents.update",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_set_check_dependents(
    request: Request,
    check_id: UUID,
    data: AdminCheckDependents,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> DependentsChange:
    """Make exactly these checks depend on this one; any other is detached."""
    return await AdminCheckCoreService.set_dependents(
        db, principal.user.role, check_id, data
    )
