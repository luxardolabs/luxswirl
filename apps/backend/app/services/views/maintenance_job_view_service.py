"""View-layer wrapper for the maintenance job status polling partial."""

from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AuthedUser
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.views._job_status import build_job_status_context


class MaintenanceJobViewService:
    """Owns the maintenance job_status partial's context.

    The partial is embedded by whichever surface enqueued the job (an agent
    delete, a bulk action, an import), so the web layer reaches the builder
    through here rather than through the package-private helper.
    """

    @staticmethod
    def build_status_context(
        request: Request, current_user: AuthedUser | None, job: MaintenanceJob
    ) -> dict[str, Any]:
        """Context for an already-loaded job (the enqueue path — no refetch)."""
        return build_job_status_context(request, current_user, job)

    @staticmethod
    async def build_status_partial_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser | None,
        job_id: UUID,
    ) -> dict[str, Any]:
        job = await MaintenanceJobCoreService.get_by_id(db, job_id)
        return build_job_status_context(request, current_user, job)
