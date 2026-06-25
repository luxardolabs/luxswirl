"""
Job bulk check view service — bulk check creation from job results.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.core.job_to_check_core_service import (
    BulkCheckParams,
    BulkCheckResult,
    JobToCheckCoreService,
    QuickCheckParams,
)
from app.services.core.settings_core_service import SettingsCoreService

logger = get_logger("luxswirl.web.services.job_bulk_check")


class JobBulkCheckViewService:
    """View-layer wrapper for bulk check creation from jobs."""

    @staticmethod
    async def _parse_bulk_form_data(
        db: AsyncSession, form_data: dict
    ) -> tuple[BulkCheckParams | None, str | None]:
        """
        Parse common bulk check form data into core BulkCheckParams.

        Returns (params, error_message). If error_message is not None,
        validation failed and params is None.
        """
        check_defaults = await SettingsCoreService.get_check_defaults(db)

        interval = int(form_data.get("interval") or check_defaults["interval_seconds"])
        timeout = int(form_data.get("timeout") or check_defaults["timeout_seconds"])
        retry_attempts = int(form_data.get("retry_attempts") or check_defaults["retry_attempts"])
        agent_id = form_data.get("agent_id")
        tags_str = form_data.get("tags", "")
        tags = [tag.strip() for tag in tags_str.split(",") if tag.strip()] if tags_str else []

        if not agent_id or agent_id == "":
            return None, "Agent is required. Please select an agent."

        expected_status = None
        verify_ssl = True
        if "expected_status" in form_data:
            expected_status = int(
                form_data.get("expected_status") or check_defaults["expected_status"]
            )
        if "verify_ssl" in form_data:
            verify_ssl_str = form_data.get("verify_ssl")
            verify_ssl = (
                verify_ssl_str.lower() == "true" if verify_ssl_str else check_defaults["verify_ssl"]
            )

        params = BulkCheckParams(
            interval=interval,
            timeout=timeout,
            retry_attempts=retry_attempts,
            agent_id=UUID(agent_id),
            tags=tags,
            expected_status=expected_status,
            verify_ssl=verify_ssl,
        )
        return params, None

    @staticmethod
    async def create_ping_checks_from_job(
        db: AsyncSession, job_id: UUID, form_data: dict
    ) -> tuple[BulkCheckResult | None, str | None]:
        """Parse form, then delegate to core."""
        params, error = await JobBulkCheckViewService._parse_bulk_form_data(db, form_data)
        if error:
            return None, error
        assert params is not None
        return await JobToCheckCoreService.create_ping_checks_from_job(db, job_id, params)

    @staticmethod
    async def create_web_checks_from_job(
        db: AsyncSession, job_id: UUID, form_data: dict
    ) -> tuple[BulkCheckResult | None, str | None]:
        """Parse form, then delegate to core."""
        params, error = await JobBulkCheckViewService._parse_bulk_form_data(db, form_data)
        if error:
            return None, error
        assert params is not None
        return await JobToCheckCoreService.create_web_checks_from_job(db, job_id, params)

    @staticmethod
    async def create_ssh_checks_from_job(
        db: AsyncSession, job_id: UUID, form_data: dict
    ) -> tuple[BulkCheckResult | None, str | None]:
        """Parse form, then delegate to core."""
        params, error = await JobBulkCheckViewService._parse_bulk_form_data(db, form_data)
        if error:
            return None, error
        assert params is not None
        return await JobToCheckCoreService.create_ssh_checks_from_job(db, job_id, params)

    @staticmethod
    async def create_database_checks_from_job(
        db: AsyncSession, job_id: UUID, form_data: dict
    ) -> tuple[BulkCheckResult | None, str | None]:
        """Parse form, then delegate to core."""
        params, error = await JobBulkCheckViewService._parse_bulk_form_data(db, form_data)
        if error:
            return None, error
        assert params is not None
        return await JobToCheckCoreService.create_database_checks_from_job(db, job_id, params)

    @staticmethod
    async def quick_create_check(db: AsyncSession, form_data: dict) -> tuple[bool, str | None]:
        """
        Parse form data from a per-host quick-action button (network scan UI)
        and create a single check.

        Returns (created, error_message).
        """
        check_defaults = await SettingsCoreService.get_check_defaults(db)

        agent_id = form_data.get("agent_id")
        check_type = form_data.get("check_type")
        target = form_data.get("target")
        display_name = form_data.get("display_name")

        if not agent_id:
            return False, "Agent is required. Please select an agent."
        if not check_type or not target or not display_name:
            return False, "Missing required check fields"

        tags_str = form_data.get("tags", "")
        tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []

        expected_status: int | None = None
        verify_ssl = True
        if check_type == "http":
            expected_status = int(
                form_data.get("expected_status") or check_defaults["expected_status"]
            )
            verify_ssl_str = form_data.get("verify_ssl")
            verify_ssl = (
                verify_ssl_str.lower() == "true" if verify_ssl_str else check_defaults["verify_ssl"]
            )

        params = QuickCheckParams(
            agent_id=UUID(agent_id),
            check_type=check_type,
            target=target,
            display_name=display_name,
            interval=int(form_data.get("interval") or check_defaults["interval_seconds"]),
            timeout=int(form_data.get("timeout") or check_defaults["timeout_seconds"]),
            retry_attempts=int(form_data.get("retry_attempts") or check_defaults["retry_attempts"]),
            tags=tags,
            expected_status=expected_status,
            verify_ssl=verify_ssl,
        )
        return await JobToCheckCoreService.create_single_check(db, params)
