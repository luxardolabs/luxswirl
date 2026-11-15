"""Who a maintenance job runs for, judged when it runs.

A job runs after the request that queued it; by then its owner may have been
demoted or deactivated. One who no longer holds the role the request needed
changes nothing.

Whether the request could act on a synthetic check is stored in the job's
params when it is queued, since the token that sent it (and its scopes) is
not there when the job runs. A job acts on a synthetic check only when that
was stored as true and its owner is still an active admin; a job queued
without it leaves every synthetic check alone.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.background.maintenance_worker import JobFailed
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.maintenance_job_core_service import ACTOR_IS_ADMIN
from app.services.core.user_core_service import UserCoreService


async def require_owner_may_edit(db: AsyncSession, job: MaintenanceJob) -> None:
    """Refuse the job unless its owner is, now, an active editor or admin."""
    if not await UserCoreService.is_active_editor(db, job.owner_id):
        raise JobFailed(
            "The job's owner is no longer an active editor or admin; "
            "nothing was changed"
        )


async def require_owner_is_admin(db: AsyncSession, job: MaintenanceJob) -> None:
    """Refuse the job unless its owner is, now, an active admin."""
    if not await UserCoreService.is_active_admin(db, job.owner_id):
        raise JobFailed(
            "The job's owner is no longer an active admin; nothing was changed"
        )


async def acts_as_admin(db: AsyncSession, job: MaintenanceJob) -> bool:
    """Whether the job may act on a synthetic check, judged now."""
    return job.params.get(ACTOR_IS_ADMIN) is True and (
        await UserCoreService.is_active_admin(db, job.owner_id)
    )
