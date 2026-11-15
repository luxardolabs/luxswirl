"""
Job-to-check materialization service.

Business logic for turning a completed network scan's hosts into monitoring
checks. Encodes the domain rules:
  - only a completed network_scan job's result is read
  - naming conventions (display name format per check type)
  - port classification (which ports map to which check kinds)
  - duplicate detection against existing checks for the agent, under the
    agent's row lock so two requests at once never both make a check
  - at most BULK_MAX checks per request, refused before any is made
  - which scan_params fields are honored

"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scan_check_names import PORT_CATEGORIES
from app.crud.agent_crud import AgentCRUD
from app.models.enum_model import CheckType, JobStatus, JobType
from app.schemas.admin_check_schema import BULK_MAX
from app.schemas.check_schema import BulkCheckCreateSummary, CheckCreate
from app.services.core.check_core_service import CheckCoreService
from app.services.core.job_core_service import JobCoreService
from app.services.core.network_scan_core_service import NetworkScanCoreService
from app.shared.logger import get_logger

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


# A check a scan host could become: its display name and target, or None for a
# host the scan result gives no usable address for (counted as skipped).
Candidate = tuple[str, str] | None
CheckBuilder = Callable[[str, str], CheckCreate]


def _host_port(host: str, port: int) -> str:
    return f"{host}:{port}"


def _http_url(host: str, port: int) -> str:
    protocol = "https" if port in [443, 8443] else "http"
    return f"{protocol}://{host}:{port}"


class JobToCheckCoreService:
    """Materialize monitoring checks from a completed network scan's hosts."""

    @staticmethod
    async def _scan_result(
        db: AsyncSession, job_id: UUID
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The job's result, if the job is a completed network scan with one.

        Returns (result, error_message): error_message is set, and result is
        None, for a job that is missing, has no result, is not a network_scan,
        or has not completed.
        """
        job = await JobCoreService.get_job(db, job_id)
        if not job or not job.result:
            return None, "Job not found or has no result"
        if job.job_type != JobType.NETWORK_SCAN or job.status != JobStatus.COMPLETED:
            return None, "Checks are made only from a completed network scan"
        return job.result, None

    @staticmethod
    def _port_candidates(
        result: dict[str, Any],
        category: str,
        prefix: str,
        target: Callable[[str, int], str],
    ) -> list[Candidate]:
        """One candidate per (host, port of `category`) the scan found open."""
        enriched = NetworkScanCoreService.enrich_result(result)
        hosts = enriched.get("categorized_hosts", {}).get(category, [])
        ports = PORT_CATEGORIES[category]
        candidates: list[Candidate] = []
        for host in hosts:
            target_host = host.get("hostname") or host.get("ip")
            if not target_host:
                # No usable address: skip the whole host rather than make a
                # garbage "http://None:80" target.
                candidates.append(None)
                continue
            candidates.extend(
                (
                    f"{prefix}_{target_host}_{port}".replace(".", "_"),
                    target(target_host, port),
                )
                for port in host.get("open_ports", [])
                if port in ports
            )
        return candidates

    @staticmethod
    def _tcp_builder(params: BulkCheckParams) -> CheckBuilder:
        """A TCP check with `params`' settings, from its name and target."""

        def build(name: str, target: str) -> CheckCreate:
            return CheckCreate(
                display_name=name,
                check_type=CheckType.TCP,
                target=target,
                interval_seconds=params.interval,
                timeout_seconds=params.timeout,
                retry_attempts=params.retry_attempts,
                enabled=True,
                tags=params.tags,
            )

        return build

    @staticmethod
    async def _create_new(
        db: AsyncSession,
        job_id: UUID,
        params: BulkCheckParams,
        candidates: list[Candidate],
        build: CheckBuilder,
        check_kind: str,
    ) -> tuple[BulkCheckCreateSummary | None, str | None]:
        """Create each candidate whose name the agent has no check of yet.

        The agent's row is locked before its check names are read and stays
        locked to the transaction's end, so a second request for the same
        agent waits for this one's checks and then skips them. A request that
        would make more than BULK_MAX checks is refused before any is made.
        """
        await AgentCRUD.lock(db, params.agent_id)
        existing = await CheckCoreService.list_checks_for_agent(db, params.agent_id)
        taken = {check.display_name for check in existing}

        planned: list[tuple[str, str]] = []
        skipped_count = 0
        for candidate in candidates:
            if candidate is None or candidate[0] in taken:
                skipped_count += 1
                continue
            taken.add(candidate[0])
            planned.append(candidate)

        if len(planned) > BULK_MAX:
            return None, (
                f"One request makes at most {BULK_MAX:,} checks; "
                f"this scan would make {len(planned):,}"
            )

        for name, target in planned:
            await CheckCoreService.create_check(
                db, params.agent_id, build(name, target)
            )

        logger.info(
            "Bulk created checks from job",
            extra={
                "check_kind": check_kind,
                "created_count": len(planned),
                "skipped_count": skipped_count,
                "job_id": str(job_id),
            },
        )
        return BulkCheckCreateSummary(len(planned), skipped_count), None

    @staticmethod
    async def create_ping_checks_from_job(
        db: AsyncSession, job_id: UUID, params: BulkCheckParams
    ) -> tuple[BulkCheckCreateSummary | None, str | None]:
        """Create one ping check per discovered host."""
        result, error = await JobToCheckCoreService._scan_result(db, job_id)
        if result is None:
            return None, error
        if "discovered_hosts" not in result:
            return None, "Job not found or has no discovered hosts"

        candidates: list[Candidate] = []
        for host in NetworkScanCoreService.scan_hosts(result):
            target = host.get("hostname") or host.get("ip")
            candidates.append(
                (f"ping_{target}".replace(".", "_"), target) if target else None
            )

        def build(name: str, target: str) -> CheckCreate:
            return CheckCreate(
                display_name=name,
                check_type=CheckType.PING,
                target=target,
                interval_seconds=params.interval,
                timeout_seconds=params.timeout,
                retry_attempts=params.retry_attempts,
                enabled=True,
                tags=params.tags,
            )

        return await JobToCheckCoreService._create_new(
            db, job_id, params, candidates, build, "ping"
        )

    @staticmethod
    async def create_web_checks_from_job(
        db: AsyncSession, job_id: UUID, params: BulkCheckParams
    ) -> tuple[BulkCheckCreateSummary | None, str | None]:
        """Create one HTTP check per (web-host, web-port) pair from a network scan."""
        result, error = await JobToCheckCoreService._scan_result(db, job_id)
        if result is None:
            return None, error

        def build(name: str, target: str) -> CheckCreate:
            return CheckCreate(
                display_name=name,
                check_type=CheckType.HTTP,
                target=target,
                interval_seconds=params.interval,
                timeout_seconds=params.timeout,
                retry_attempts=params.retry_attempts,
                enabled=True,
                expected_status=params.expected_status,
                verify_ssl=params.verify_ssl,
                tags=params.tags,
            )

        return await JobToCheckCoreService._create_new(
            db,
            job_id,
            params,
            JobToCheckCoreService._port_candidates(result, "web", "http", _http_url),
            build,
            "http",
        )

    @staticmethod
    async def create_ssh_checks_from_job(
        db: AsyncSession, job_id: UUID, params: BulkCheckParams
    ) -> tuple[BulkCheckCreateSummary | None, str | None]:
        """Create one TCP check per (ssh-host, ssh-port) pair from a network scan."""
        result, error = await JobToCheckCoreService._scan_result(db, job_id)
        if result is None:
            return None, error
        return await JobToCheckCoreService._create_new(
            db,
            job_id,
            params,
            JobToCheckCoreService._port_candidates(result, "ssh", "ssh", _host_port),
            JobToCheckCoreService._tcp_builder(params),
            "ssh",
        )

    @staticmethod
    async def create_database_checks_from_job(
        db: AsyncSession, job_id: UUID, params: BulkCheckParams
    ) -> tuple[BulkCheckCreateSummary | None, str | None]:
        """Create one TCP check per (db-host, db-port) pair from a network scan."""
        result, error = await JobToCheckCoreService._scan_result(db, job_id)
        if result is None:
            return None, error
        return await JobToCheckCoreService._create_new(
            db,
            job_id,
            params,
            JobToCheckCoreService._port_candidates(
                result, "database", "db", _host_port
            ),
            JobToCheckCoreService._tcp_builder(params),
            "database",
        )

    @staticmethod
    async def create_single_check(
        db: AsyncSession, params: QuickCheckParams
    ) -> tuple[bool, str | None]:
        """
        Create one check from a per-host quick-action button (network scan UI).

        Returns (created, error_message). `created` is False (with error_message
        set) if the check name already exists for the agent or validation fails.
        The agent's row is locked before its names are read, as in the bulk
        creators, so two clicks at once make the check once.
        """
        await AgentCRUD.lock(db, params.agent_id)
        existing_checks = await CheckCoreService.list_checks_for_agent(
            db, params.agent_id
        )
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
            expected_status=params.expected_status
            if params.check_type == "http"
            else None,
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
