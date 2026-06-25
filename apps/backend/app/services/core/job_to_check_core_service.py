"""
Job-to-check materialization service.

Business logic for turning network scan / discover job results into actual
monitoring checks. Encodes the domain rules:
  - naming conventions (display name format per check type)
  - port classification (which ports map to which check kinds)
  - duplicate detection against existing checks for the agent
  - which scan_params fields are honored

"""

from dataclasses import dataclass
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enum_model import CheckType
from app.schemas.check_schema import CheckCreate
from app.services.core.check_core_service import CheckCoreService
from app.services.core.job_core_service import JobCoreService
from app.services.core.network_scan_core_service import NetworkScanCoreService

logger = get_logger("luxswirl.services.job_to_check")


@dataclass
class BulkCheckParams:
    """Parameters describing how to materialize checks from a job result."""

    interval: int
    timeout: int
    retry_attempts: int
    agent_id: UUID
    tags: list[str]
    expected_status: int | None = None
    verify_ssl: bool = True


@dataclass
class BulkCheckResult:
    """Outcome of a bulk-create operation."""

    created_count: int
    skipped_count: int


@dataclass
class QuickCheckParams:
    """Parameters for creating a single check from a per-host quick-action button."""

    agent_id: UUID
    check_type: str
    target: str
    display_name: str
    interval: int
    timeout: int
    retry_attempts: int
    tags: list[str]
    expected_status: int | None = None
    verify_ssl: bool = True


class JobToCheckCoreService:
    """Materialize monitoring checks from network scan / discover job results."""

    @staticmethod
    async def create_ping_checks_from_job(
        db: AsyncSession, job_id: UUID, params: BulkCheckParams
    ) -> tuple[BulkCheckResult | None, str | None]:
        """Create one ping check per discovered host."""
        job = await JobCoreService.get_job(db, job_id)
        if not job or not job.result or "discovered_hosts" not in job.result:
            return None, "Job not found or has no discovered hosts"

        discovered_hosts = job.result.get("discovered_hosts", [])
        existing_checks = await CheckCoreService.list_checks_for_agent(db, params.agent_id)
        existing_check_names = {check.display_name for check in existing_checks}

        created_count = 0
        skipped_count = 0

        for host in discovered_hosts:
            ip = host.get("ip")
            hostname = host.get("hostname")
            target = hostname or ip
            if not target:
                # Partial scan result (no ip or hostname). Skip it rather than
                # let CheckCreate(target=None) crash the whole batch.
                skipped_count += 1
                continue
            check_name = f"ping_{target}".replace(".", "_")

            if check_name in existing_check_names:
                skipped_count += 1
                continue

            check_data = CheckCreate(
                display_name=check_name,
                check_type=CheckType.PING,
                target=target,
                interval_seconds=params.interval,
                timeout_seconds=params.timeout,
                retry_attempts=params.retry_attempts,
                enabled=True,
                tags=params.tags,
            )
            await CheckCoreService.create_check(db, params.agent_id, check_data)
            created_count += 1

        logger.info(
            "Bulk created ping checks from job",
            extra={
                "created_count": created_count,
                "skipped_count": skipped_count,
                "job_id": str(job_id),
            },
        )
        return BulkCheckResult(created_count, skipped_count), None

    @staticmethod
    async def create_web_checks_from_job(
        db: AsyncSession, job_id: UUID, params: BulkCheckParams
    ) -> tuple[BulkCheckResult | None, str | None]:
        """Create one HTTP check per (web-host, web-port) pair from a network scan."""
        job = await JobCoreService.get_job(db, job_id)
        if not job or not job.result:
            return None, "Job not found or has no result"

        result = NetworkScanCoreService.enrich_result(job.result)
        web_hosts = result.get("categorized_hosts", {}).get("web", [])

        existing_checks = await CheckCoreService.list_checks_for_agent(db, params.agent_id)
        existing_check_names = {check.display_name for check in existing_checks}

        web_ports = [80, 443, 8080, 8443, 8000, 3000, 5000]
        created_count = 0
        skipped_count = 0

        for host in web_hosts:
            ip = host.get("ip")
            hostname = host.get("hostname")
            target_host = hostname or ip
            if not target_host:
                # Partial scan result — skip the whole host (avoids a garbage
                # "http://None:80" target).
                skipped_count += 1
                continue
            for port in host.get("open_ports", []):
                if port not in web_ports:
                    continue
                protocol = "https" if port in [443, 8443] else "http"
                check_name = f"http_{target_host}_{port}".replace(".", "_")

                if check_name in existing_check_names:
                    skipped_count += 1
                    continue

                check_data = CheckCreate(
                    display_name=check_name,
                    check_type=CheckType.HTTP,
                    target=f"{protocol}://{target_host}:{port}",
                    interval_seconds=params.interval,
                    timeout_seconds=params.timeout,
                    retry_attempts=params.retry_attempts,
                    enabled=True,
                    expected_status=params.expected_status,
                    verify_ssl=params.verify_ssl,
                    tags=params.tags,
                )
                await CheckCoreService.create_check(db, params.agent_id, check_data)
                created_count += 1

        logger.info(
            "Bulk created HTTP checks from job",
            extra={
                "created_count": created_count,
                "skipped_count": skipped_count,
                "job_id": str(job_id),
            },
        )
        return BulkCheckResult(created_count, skipped_count), None

    @staticmethod
    async def create_ssh_checks_from_job(
        db: AsyncSession, job_id: UUID, params: BulkCheckParams
    ) -> tuple[BulkCheckResult | None, str | None]:
        """Create one TCP check per (ssh-host, ssh-port) pair from a network scan."""
        job = await JobCoreService.get_job(db, job_id)
        if not job or not job.result:
            return None, "Job not found or has no result"

        result = NetworkScanCoreService.enrich_result(job.result)
        ssh_hosts = result.get("categorized_hosts", {}).get("ssh", [])

        existing_checks = await CheckCoreService.list_checks_for_agent(db, params.agent_id)
        existing_check_names = {check.display_name for check in existing_checks}

        ssh_ports = [22, 2222]
        created_count = 0
        skipped_count = 0

        for host in ssh_hosts:
            ip = host.get("ip")
            hostname = host.get("hostname")
            target_host = hostname or ip
            if not target_host:
                # Partial scan result — skip the whole host.
                skipped_count += 1
                continue
            for port in host.get("open_ports", []):
                if port not in ssh_ports:
                    continue
                check_name = f"ssh_{target_host}_{port}".replace(".", "_")

                if check_name in existing_check_names:
                    skipped_count += 1
                    continue

                check_data = CheckCreate(
                    display_name=check_name,
                    check_type=CheckType.TCP,
                    target=f"{target_host}:{port}",
                    interval_seconds=params.interval,
                    timeout_seconds=params.timeout,
                    retry_attempts=params.retry_attempts,
                    enabled=True,
                    tags=params.tags,
                )
                await CheckCoreService.create_check(db, params.agent_id, check_data)
                created_count += 1

        logger.info(
            "Bulk created SSH checks from job",
            extra={
                "created_count": created_count,
                "skipped_count": skipped_count,
                "job_id": str(job_id),
            },
        )
        return BulkCheckResult(created_count, skipped_count), None

    @staticmethod
    async def create_single_check(
        db: AsyncSession, params: QuickCheckParams
    ) -> tuple[bool, str | None]:
        """
        Create one check from a per-host quick-action button (network scan UI).

        Returns (created, error_message). `created` is False (with error_message
        set) if the check name already exists for the agent or validation fails.
        """
        existing_checks = await CheckCoreService.list_checks_for_agent(db, params.agent_id)
        if any(check.display_name == params.display_name for check in existing_checks):
            return False, "Check already exists"

        check_data = CheckCreate(
            display_name=params.display_name,
            check_type=CheckType(params.check_type),
            target=params.target,
            interval_seconds=params.interval,
            timeout_seconds=params.timeout,
            retry_attempts=params.retry_attempts,
            enabled=True,
            expected_status=params.expected_status if params.check_type == "http" else None,
            verify_ssl=params.verify_ssl if params.check_type == "http" else True,
            tags=params.tags,
        )
        await CheckCoreService.create_check(db, params.agent_id, check_data)
        logger.info(
            "Quick-created single check",
            extra={
                "check_type": params.check_type,
                "display_name": params.display_name,
                "agent_id": str(params.agent_id),
            },
        )
        return True, None

    @staticmethod
    async def create_database_checks_from_job(
        db: AsyncSession, job_id: UUID, params: BulkCheckParams
    ) -> tuple[BulkCheckResult | None, str | None]:
        """Create one TCP check per (db-host, db-port) pair from a network scan."""
        job = await JobCoreService.get_job(db, job_id)
        if not job or not job.result:
            return None, "Job not found or has no result"

        result = NetworkScanCoreService.enrich_result(job.result)
        db_hosts = result.get("categorized_hosts", {}).get("database", [])

        existing_checks = await CheckCoreService.list_checks_for_agent(db, params.agent_id)
        existing_check_names = {check.display_name for check in existing_checks}

        db_ports = [3306, 5432, 5433, 27017, 6379, 1433, 1521]
        created_count = 0
        skipped_count = 0

        for host in db_hosts:
            ip = host.get("ip")
            hostname = host.get("hostname")
            target_host = hostname or ip
            if not target_host:
                # Partial scan result — skip the whole host.
                skipped_count += 1
                continue
            for port in host.get("open_ports", []):
                if port not in db_ports:
                    continue
                check_name = f"db_{target_host}_{port}".replace(".", "_")

                if check_name in existing_check_names:
                    skipped_count += 1
                    continue

                check_data = CheckCreate(
                    display_name=check_name,
                    check_type=CheckType.TCP,
                    target=f"{target_host}:{port}",
                    interval_seconds=params.interval,
                    timeout_seconds=params.timeout,
                    retry_attempts=params.retry_attempts,
                    enabled=True,
                    tags=params.tags,
                )
                await CheckCoreService.create_check(db, params.agent_id, check_data)
                created_count += 1

        logger.info(
            "Bulk created database checks from job",
            extra={
                "created_count": created_count,
                "skipped_count": skipped_count,
                "job_id": str(job_id),
            },
        )
        return BulkCheckResult(created_count, skipped_count), None
