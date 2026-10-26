"""Who a destructive maintenance job runs for, judged when it runs.

A job runs after the request that queued it; by then its owner may have been
demoted or deactivated. One no longer an active editor or admin changes
nothing.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.background.maintenance_worker import JobFailed
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.user_core_service import UserCoreService


async def require_owner_may_edit(db: AsyncSession, job: MaintenanceJob) -> None:
    """Refuse the job unless its owner is, now, an active editor or admin."""
    if not await UserCoreService.is_active_editor(db, job.owner_id):
        raise JobFailed(
            "The job's owner is no longer an active editor or admin; "
            "nothing was changed"
        )
