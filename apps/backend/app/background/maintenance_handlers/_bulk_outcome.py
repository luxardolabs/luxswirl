"""What a bulk check job keeps when it has run.

Its outcome (counts and first errors) is kept on the job for whoever polls
it, whether the job is done or failed.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.background.maintenance_worker import JobFailed
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService

# The most per-check errors a job keeps; the counts are always whole.
_ERRORS_KEPT = 50


async def finish(
    db: AsyncSession,
    job: MaintenanceJob,
    verb: str,
    result: dict[str, Any],
    *,
    fail_when_nothing_done: bool,
) -> None:
    """Keep `result`'s counts and first errors as the job's progress.

    `verb` names what was done to a check ("Deleted", "Enabled", ...). With
    `fail_when_nothing_done`, a job that changed no check and failed some
    fails, its progress kept beside the failure.
    """
    done = result["success_count"]
    failed = result["failure_count"]
    total = done + failed
    message = f"{verb} {done} of {total} check(s)"
    if failed:
        message += f"; {failed} failed"
    progress = {
        "message": message,
        "processed": total,
        "total": total,
        "success_count": done,
        "failure_count": failed,
        "errors": [
            MaintenanceJobCoreService.readable_error(e)
            for e in result["errors"][:_ERRORS_KEPT]
        ],
    }
    if fail_when_nothing_done and done == 0 and failed:
        raise JobFailed("; ".join(progress["errors"]), progress)
    await MaintenanceJobCoreService.update_progress(db, job.id, progress)
