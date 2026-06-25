"""
Scheduler initialization - seed default job configurations.
"""

from shared.logger import get_logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger("luxswirl.db.scheduler_init")


async def init_scheduler_defaults(db: AsyncSession) -> None:
    """
    Initialize scheduler with default job configurations.

    Idempotent - only creates jobs that don't already exist.

    Note: This function does NOT commit - the caller must commit the transaction.
    """
    try:
        from app.db.defaults.scheduler_templates import DEFAULT_JOB_CONFIGURATIONS
        from app.models.scheduler_model import JobConfiguration

        # Create job configurations
        configs_created = 0
        for config_data in DEFAULT_JOB_CONFIGURATIONS:
            result = await db.execute(
                select(JobConfiguration).where(JobConfiguration.job_key == config_data["job_key"])
            )
            existing = result.scalar_one_or_none()

            if not existing:
                config = JobConfiguration(**config_data)
                db.add(config)
                configs_created += 1

        # Flush changes - caller will commit
        await db.flush()

        logger.info(
            "Scheduler defaults initialized",
            extra={"configs_created": configs_created},
        )
    except Exception as e:
        logger.error("Failed to initialize scheduler defaults", extra={"error": str(e)})
        raise
