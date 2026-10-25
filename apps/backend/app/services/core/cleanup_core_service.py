"""
Cleanup job functions for the LuxSwirl scheduler.

Each cleanup function delegates to its corresponding CRUD module — no
direct database access in this service. Artifacts and notification logs are
kept for as long as their retention settings say; nothing else deletes them.
"""

from datetime import timedelta
from typing import Any

from app.core.datetime_utils import utc_now
from app.crud.agent_crud import AgentCRUD
from app.crud.artifact_crud import ArtifactCRUD
from app.crud.notification_log_crud import NotificationLogCRUD
from app.crud.scheduler_crud import JobExecutionCRUD
from app.crud.session_crud import SessionCRUD
from app.db import worker_session
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.scheduler.cleanup")


async def cleanup_old_job_executions(days_to_keep: int = 90) -> dict[str, Any]:
    """Delete job execution records older than N days."""
    async with worker_session() as db:
        cutoff = utc_now() - timedelta(days=days_to_keep)
        deleted = await JobExecutionCRUD.delete_before(db, cutoff)
        logger.info(
            "Cleaned up old job executions",
            extra={"deleted": deleted, "days_to_keep": days_to_keep},
        )
        return {"deleted": deleted, "days_to_keep": days_to_keep}


async def cleanup_notification_logs() -> dict[str, Any]:
    """Delete notification logs older than `database.notification_logs_retention_days`.

    0 keeps every log.
    """
    async with worker_session() as db:
        days_to_keep = await SettingsCoreService.get_setting(
            db, "database.notification_logs_retention_days", 90
        )
        if days_to_keep <= 0:
            return {"deleted": 0, "days_to_keep": days_to_keep}
        cutoff = utc_now() - timedelta(days=days_to_keep)
        deleted = await NotificationLogCRUD.delete_older_than(db, cutoff)
        logger.info(
            "Cleaned up old notification logs",
            extra={"deleted": deleted, "days_to_keep": days_to_keep},
        )
        return {"deleted": deleted, "days_to_keep": days_to_keep}


async def cleanup_stale_agents(inactive_days: int = 30) -> dict[str, Any]:
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


async def cleanup_check_artifacts() -> dict[str, Any]:
    """Delete check artifacts older than `database.artifacts_retention_days`."""
    async with worker_session() as db:
        days_to_keep = await SettingsCoreService.get_setting(
            db, "database.artifacts_retention_days", 30
        )
        cutoff = utc_now() - timedelta(days=days_to_keep)
        deleted = await ArtifactCRUD.delete_older_than(db, cutoff)
        logger.info(
            "Cleaned up old check artifacts",
            extra={"deleted": deleted, "days_to_keep": days_to_keep},
        )
        return {"deleted": deleted, "days_to_keep": days_to_keep}


async def cleanup_old_sessions(days_to_keep: int = 7) -> dict[str, Any]:
    """Delete expired user sessions older than N days."""
    async with worker_session() as db:
        cutoff = utc_now() - timedelta(days=days_to_keep)
        deleted = await SessionCRUD.delete_expired_before(db, cutoff)
        logger.info(
            "Cleaned up old sessions",
            extra={"deleted": deleted, "days_to_keep": days_to_keep},
        )
        return {"deleted": deleted, "days_to_keep": days_to_keep}
