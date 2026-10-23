"""
Scheduler initialization - seed default job configurations.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.scheduler_crud import JobConfigurationCRUD
from app.db.defaults.scheduler_templates import DEFAULT_JOB_CONFIGURATIONS
from app.models.scheduler_model import JobConfiguration
from app.shared.logger import get_logger

logger = get_logger("luxswirl.db.scheduler_init")


async def init_scheduler_defaults(db: AsyncSession) -> None:
    """
    Initialize scheduler with default job configurations.

    Idempotent - only creates jobs that don't already exist.

    Note: This function does NOT commit - the caller must commit the transaction.
    """
    try:
        # Only the defaults that do not exist yet: seeding is idempotent.
        missing = [
            JobConfiguration(**config_data)
            for config_data in DEFAULT_JOB_CONFIGURATIONS
            if await JobConfigurationCRUD.get_by_job_key(db, config_data["job_key"])
            is None
        ]
        await JobConfigurationCRUD.add_all(db, missing)
        configs_created = len(missing)

        logger.info(
            "Scheduler defaults initialized",
            extra={"configs_created": configs_created},
        )
    except Exception as e:
        logger.exception(
            "Failed to initialize scheduler defaults", extra={"error": str(e)}
        )
        raise
