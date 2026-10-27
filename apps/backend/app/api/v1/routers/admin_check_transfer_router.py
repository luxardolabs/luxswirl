"""
Admin API check transfer router - an agent's checks, exported and imported.

Both take an admin's token, as the web UI's import/export panel does: an
export carries every check's target, credentials included, and an import may
replace any of the agent's checks.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiAdmin, require_scope
from app.core.config import settings
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_check_schema import AdminCheckImport
from app.schemas.import_export_schema import CheckExportDocument
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.services.core.check_transfer_core_service import CheckTransferCoreService

router = APIRouter(
    prefix="/agents/{agent_id}/checks",
    tags=["Admin API: Checks"],
    dependencies=[Depends(require_scope("checks", admin_writes=True))],
)

LIMIT = settings.security.admin_api_rate_limit


@router.get(
    "/export", response_model=CheckExportDocument, name="admin_export_agent_checks"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_export_agent_checks(
    request: Request,
    agent_id: UUID,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> CheckExportDocument:
    """The agent's checks in the form `import` takes back."""
    return await CheckTransferCoreService.export_document(db, agent_id)


@router.post(
    "/import",
    response_model=MaintenanceJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    name="admin_import_agent_checks",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_import_agent_checks(
    request: Request,
    agent_id: UUID,
    data: AdminCheckImport,
    principal: ApiAdmin,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> MaintenanceJobResponse:
    """Import checks onto the agent in the background. Poll
    `/api/v1/admin/maintenance-jobs/{id}` for the outcome.
    """
    return await CheckTransferCoreService.enqueue_import_checks(
        db, principal.user.id, agent_id, data.mode, data.checks
    )
