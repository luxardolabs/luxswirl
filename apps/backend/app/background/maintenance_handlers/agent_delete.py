"""Handler for agent_delete maintenance jobs.

Runs in the worker session (statement_timeout already lifted). Delegates to the
existing AgentCoreService.delete_agent for the actual cascade so the business
logic stays in one place.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.user_core_service import UserCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.maintenance.agent_delete")


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    if job.target_id is None:
        raise ValueError("agent_delete job requires target_id")

    agent_id: UUID = job.target_id
    logger.info(
        "Deleting agent (cascade)",
        extra={"agent_id": str(agent_id), "job_id": str(job.id)},
    )

    try:
        await AgentCoreService.delete_agent(
            db,
            agent_id,
            actor_is_admin=await UserCoreService.is_active_admin(db, job.owner_id),
        )
    except NotFoundException:
        logger.warning(
            "Agent already gone — marking job done",
            extra={"agent_id": str(agent_id), "job_id": str(job.id)},
        )
        # Idempotent — already-deleted is the same outcome the user asked for.
        return
