"""
Cleanup job functions for the LuxSwirl scheduler.

Each cleanup function delegates to its corresponding CRUD module — no
direct database access in this service.
"""

from datetime import timedelta

from shared.logger import get_logger

from app.core.datetime_utils import utc_now
from app.crud.agent_crud import AgentCRUD
from app.crud.artifact_crud import ArtifactCRUD
from app.crud.notification_log_crud import NotificationLogCRUD
from app.crud.scheduler_crud import JobExecutionCRUD
from app.crud.session_crud import SessionCRUD
from app.db import worker_session

logger = get_logger("luxswirl.scheduler.cleanup")


async def cleanup_old_job_executions(days_to_keep: int = 90) -> dict:
    """Delete job execution records older than N days."""
    async with worker_session() as db:
        cutoff = utc_now() - timedelta(days=days_to_keep)
        deleted = await JobExecutionCRUD.delete_before(db, cutoff)
        logger.info(
            "Cleaned up old job executions",
            extra={"deleted": deleted, "days_to_keep": days_to_keep},
        )
        return {"deleted": deleted, "days_to_keep": days_to_keep}


async def cleanup_notification_logs(days_to_keep: int = 30) -> dict:
    """Delete notification log records older than N days."""
    async with worker_session() as db:
        cutoff = utc_now() - timedelta(days=days_to_keep)
        deleted = await NotificationLogCRUD.delete_older_than(db, cutoff)
        logger.info(
            "Cleaned up old notification logs",
            extra={"deleted": deleted, "days_to_keep": days_to_keep},
        )
        return {"deleted": deleted, "days_to_keep": days_to_keep}


async def cleanup_stale_agents(inactive_days: int = 30) -> dict:
    """Remove agents that haven't reported in N days (cascades to checks/results)."""
    async with worker_session() as db:
        cutoff = utc_now() - timedelta(days=inactive_days)
        stale_names = await AgentCRUD.get_stale_agent_names(db, cutoff)
        if stale_names:
            deleted = await AgentCRUD.delete_stale_agents(db, cutoff)
            logger.info(
                "Cleaned up stale agents",
                extra={
                    "deleted": deleted,
                    "inactive_days": inactive_days,
                    "agents": stale_names,
                },
            )
        else:
            deleted = 0
        return {
            "deleted": deleted,
            "inactive_days": inactive_days,
            "agents": stale_names,
        }


async def cleanup_check_artifacts(days_to_keep: int = 30) -> dict:
    """Delete check artifacts older than N days."""
    async with worker_session() as db:
        cutoff = utc_now() - timedelta(days=days_to_keep)
        deleted = await ArtifactCRUD.delete_older_than(db, cutoff)
        logger.info(
            "Cleaned up old check artifacts",
            extra={"deleted": deleted, "days_to_keep": days_to_keep},
        )
        return {"deleted": deleted, "days_to_keep": days_to_keep}


async def cleanup_old_sessions(days_to_keep: int = 7) -> dict:
    """Delete expired user sessions older than N days."""
    async with worker_session() as db:
        cutoff = utc_now() - timedelta(days=days_to_keep)
        deleted = await SessionCRUD.delete_expired_before(db, cutoff)
        logger.info(
            "Cleaned up old sessions",
            extra={"deleted": deleted, "days_to_keep": days_to_keep},
        )
        return {"deleted": deleted, "days_to_keep": days_to_keep}
