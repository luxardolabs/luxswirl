"""View-layer wrapper for the maintenance job status polling partial."""

from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_model import User
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService


class MaintenanceJobViewService:
    @staticmethod
    async def build_status_partial_context(
        db: AsyncSession,
        request: Request,
        current_user: User | None,
        job_id: UUID,
    ) -> dict[str, Any]:
        job = await MaintenanceJobCoreService.get_by_id(db, job_id)
        return {
            "request": request,
            "current_user": current_user,
            "job": job,
        }
