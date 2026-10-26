"""A bulk check job's outcome, kept on the job for whoever polls it.

The worker marks the job done or failed and nothing more, so without this the
checks a job refused or could not find are visible only in the log.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService

# The most per-check errors a job keeps; the counts are always whole.
_ERRORS_KEPT = 50


async def record_outcome(
    db: AsyncSession, job: MaintenanceJob, verb: str, result: dict[str, Any]
) -> None:
    """Store `result`'s counts and first errors as the job's progress.

    `verb` names what was done to a check ("Deleted", "Enabled", ...).
    """
    done = result["success_count"]
    failed = result["failure_count"]
    total = done + failed
    message = f"{verb} {done} of {total} check(s)"
    if failed:
        message += f"; {failed} failed"
    await MaintenanceJobCoreService.update_progress(
        db,
        job.id,
        {
            "message": message,
            "processed": total,
            "total": total,
            "success_count": done,
            "failure_count": failed,
            "errors": result["errors"][:_ERRORS_KEPT],
        },
    )
