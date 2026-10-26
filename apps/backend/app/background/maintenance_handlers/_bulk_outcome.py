"""What a bulk check job checks before it runs, and keeps after.

A job runs after the request that queued it, so its owner is judged again
when it runs: one no longer an active editor or admin changes nothing. Its
outcome (counts and first errors) is kept on the job for whoever polls it,
whether the job is done or failed.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.background.maintenance_worker import JobFailed
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.core.user_core_service import UserCoreService

# The most per-check errors a job keeps; the counts are always whole.
_ERRORS_KEPT = 50


async def require_owner_may_edit(db: AsyncSession, job: MaintenanceJob) -> None:
    """Refuse the job unless its owner is, now, an active editor or admin."""
    if not await UserCoreService.is_active_editor(db, job.owner_id):
        raise JobFailed(
            "The job's owner may no longer change checks; nothing was changed"
        )


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
        "errors": result["errors"][:_ERRORS_KEPT],
    }
    if fail_when_nothing_done and done == 0 and failed:
        raise JobFailed("; ".join(result["errors"]), progress)
    await MaintenanceJobCoreService.update_progress(db, job.id, progress)
