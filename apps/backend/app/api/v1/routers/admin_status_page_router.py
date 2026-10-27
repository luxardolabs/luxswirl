"""
Admin API status page router - status pages, addressed by their own id.

Reads take any API token; every change takes an editor's or an admin's, as in
the web UI.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiEditor, ApiUser
from app.core.config import settings
from app.core.query_params import PublicFilter
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_page_schema import MAX_PAGE
from app.schemas.admin_status_page_schema import (
    AdminStatusPage,
    AdminStatusPageCreate,
    AdminStatusPageItems,
    AdminStatusPageUpdate,
    StatusPagePage,
)
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.services.core.admin_status_page_core_service import (
    AdminStatusPageCoreService,
)

router = APIRouter(prefix="/status-pages", tags=["Admin API: Status pages"])

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=StatusPagePage, name="admin_list_status_pages")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_status_pages(
    request: Request,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    is_public: PublicFilter = None,
) -> StatusPagePage:
    """One page of status pages by name; only public or private ones with
    `is_public`."""
    return await AdminStatusPageCoreService.list_pages(
        db, page=page, per_page=per_page, is_public=is_public
    )


@router.get(
    "/{status_page_id}", response_model=AdminStatusPage, name="admin_get_status_page"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_status_page(
    request: Request,
    status_page_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminStatusPage:
    return await AdminStatusPageCoreService.get_page(db, status_page_id)


@router.post(
    "",
    response_model=AdminStatusPage,
    status_code=status.HTTP_201_CREATED,
    name="admin_create_status_page",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_create_status_page(
    request: Request,
    data: AdminStatusPageCreate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminStatusPage:
    """A new page, private unless `is_public`; a taken slug is a 409."""
    return await AdminStatusPageCoreService.create_page(db, data)


@router.patch(
    "/{status_page_id}",
    response_model=AdminStatusPage,
    name="admin_update_status_page",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_update_status_page(
    request: Request,
    status_page_id: UUID,
    data: AdminStatusPageUpdate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminStatusPage:
    """Change the fields sent."""
    return await AdminStatusPageCoreService.update_page(db, status_page_id, data)


@router.put(
    "/{status_page_id}/items",
    response_model=AdminStatusPage,
    name="admin_set_status_page_items",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_set_status_page_items(
    request: Request,
    status_page_id: UUID,
    data: AdminStatusPageItems,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AdminStatusPage:
    """Make exactly these items the page's, in this order."""
    return await AdminStatusPageCoreService.replace_items(db, status_page_id, data)


@router.delete(
    "/{status_page_id}",
    response_model=MaintenanceJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    name="admin_delete_status_page",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_delete_status_page(
    request: Request,
    status_page_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> MaintenanceJobResponse:
    """Delete the page in the background.

    Poll `/api/v1/admin/maintenance-jobs/{id}` for the outcome.
    """
    return await AdminStatusPageCoreService.delete_page(
        db, status_page_id, principal.user.id
    )
