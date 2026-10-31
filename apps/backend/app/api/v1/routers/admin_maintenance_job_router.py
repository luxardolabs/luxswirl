"""
Admin API maintenance-job router - the status of a background job.

A bulk delete or modify answers 202 with its job; this is where its outcome is
read. Any API token reads any job, as any signed-in user polls one in the UI.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiUser, require_scope
from app.core.config import settings
from app.core.openapi_operations import HandlerNamedRoute
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService

router = APIRouter(
    route_class=HandlerNamedRoute,
    prefix="/maintenance-jobs",
    tags=["Admin API: Maintenance jobs"],
    dependencies=[Depends(require_scope("jobs"))],
)

LIMIT = settings.security.admin_api_rate_limit


@router.get(
    "/{job_id}",
    response_model=MaintenanceJobResponse,
    name="api.admin.maintenance_jobs.show",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_maintenance_job(
    request: Request,
    job_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> MaintenanceJobResponse:
    """The job's status, and once it has run, its counts and first errors."""
    return await MaintenanceJobCoreService.get_by_id(db, job_id)
