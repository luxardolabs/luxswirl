"""Handler for status_page_delete maintenance jobs."""

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.status_page_core_service import StatusPageCoreService

logger = get_logger("luxswirl.maintenance.status_page_delete")


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    if job.target_id is None:
        raise ValueError("status_page_delete job requires target_id")
    logger.info(
        "Deleting status page (cascade)",
        extra={"status_page_id": str(job.target_id), "job_id": str(job.id)},
    )
    try:
        await StatusPageCoreService.delete_status_page(db, job.target_id)
    except NotFoundException:
        logger.warning("Status page already gone — marking done")
        return
