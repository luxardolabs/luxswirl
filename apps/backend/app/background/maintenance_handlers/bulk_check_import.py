"""Handler for bulk_check_import maintenance jobs.

job.params expects {"agent_id": str, "mode": "merge"|"replace", "checks": [...]}.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.views.import_export_view_service import ImportExportViewService

logger = get_logger("luxswirl.maintenance.bulk_check_import")


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    agent_id_raw = job.params.get("agent_id")
    checks_data = job.params.get("checks") or []
    mode = job.params.get("mode") or "merge"

    if not agent_id_raw:
        raise ValueError("bulk_check_import job requires params.agent_id")
    if not checks_data:
        logger.info("bulk_check_import with empty checks — nothing to do")
        return

    agent_id = UUID(agent_id_raw) if isinstance(agent_id_raw, str) else agent_id_raw

    logger.info(
        "Importing checks",
        extra={
            "agent_id": str(agent_id),
            "count": len(checks_data),
            "mode": mode,
            "job_id": str(job.id),
        },
    )

    await MaintenanceJobCoreService.update_progress(
        db, job.id, {"message": f"Importing {len(checks_data)} check(s)", "total": len(checks_data)}
    )

    result = await ImportExportViewService.import_checks_from_data(db, agent_id, checks_data, mode)

    logger.info(
        "Bulk check import complete",
        extra={
            "agent_id": str(agent_id),
            "created": result.created,
            "updated": result.updated,
            "skipped": result.skipped,
            "errors_count": len(result.errors) if result.errors else 0,
            "job_id": str(job.id),
        },
    )
