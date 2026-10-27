"""Handler for status_page_delete maintenance jobs."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.background.maintenance_handlers._job_owner import require_owner_may_edit
from app.core.exceptions import NotFoundException
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.status_page_core_service import StatusPageCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.maintenance.status_page_delete")


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    if job.target_id is None:
        raise ValueError("status_page_delete job requires target_id")
    await require_owner_may_edit(db, job)
    logger.info(
        "Deleting status page (cascade)",
        extra={"status_page_id": str(job.target_id), "job_id": str(job.id)},
    )
    try:
        await StatusPageCoreService.delete_status_page(db, job.target_id)
    except NotFoundException:
        logger.warning("Status page already gone — marking done")
        return
