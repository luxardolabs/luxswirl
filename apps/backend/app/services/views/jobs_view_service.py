"""
Jobs service - aggregates job data for web UI.

Provides data specifically formatted for web UI consumption.
This web service acts as an aggregation layer, delegating to core services.
"""

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.core.scan_panel import build_scan_view
from app.models.enum_model import SERVER_RUNNER, JobStatus, JobType
from app.schemas.auth_schema import AuthedUser
from app.schemas.job_schema import JobCreate, JobInDB, JobUrls, ScanActionUrls
from app.schemas.pagination_schema import build_pagination, paginated_url
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.job_api_core_service import JobApiCoreService
from app.services.core.job_core_service import JobCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.jobs.base import BaseJob
from app.shared.jobs.network_discover import NetworkDiscoverJob
from app.shared.jobs.network_scan import NetworkScanJob
from app.shared.logger import get_logger

_JOB_REGISTRY: dict[str, type[BaseJob]] = {
    "network_scan": NetworkScanJob,
    "network_discover": NetworkDiscoverJob,
}

logger = get_logger("luxswirl.web.services.jobs")


def build_job_urls(request: Request, job_id: UUID) -> JobUrls:
    # .path: url_for returns an absolute url from the request's scheme/host, which
    # behind TLS-terminating nginx can emit http:// into an https:// page.
    def u(name: str) -> str:
        return str(request.url_for(name, job_id=job_id).path)

    return JobUrls(
        detail=u("job_detail_panel"),
        cancel=u("cancel_job_web"),
        delete=u("delete_job_web"),
    )


def build_scan_action_urls(request: Request, job_id: UUID) -> ScanActionUrls:
    # .path: url_for returns an absolute url from the request's scheme/host, which
    # behind TLS-terminating nginx can emit http:// into an https:// page.
    def u(name: str) -> str:
        return str(request.url_for(name, job_id=job_id).path)

    return ScanActionUrls(
        bulk_ping=u("bulk_create_ping_checks"),
        bulk_web=u("bulk_create_web_checks"),
        bulk_ssh=u("bulk_create_ssh_checks"),
        bulk_database=u("bulk_create_database_checks"),
        quick_create=u("quick_create_check_from_host"),
    )


def build_discover_view(request: Request, job: JobInDB) -> dict[str, Any]:
    """A network_discover result's "scan this subnet" links, on the things they scan.

    One scan is offered per discovered subnet: the inferred network, and each
    interface's suggested_scan. Each link is a prefilled create-form href; the
    prefill payload is query state, so it is encoded here rather than
    concatenated in the template. Each interface comes back with its own
    `scan_url` (None when it suggests no scan).
    """
    result = job.result or {}
    base = request.url_for("job_create_form").path

    def form_url(subnet: str | None) -> str | None:
        if not subnet:
            return None
        query = urlencode(
            {
                "job_type": "network_scan",
                "agent_id": job.runner_token,
                "prefill_params": json.dumps({"subnet": subnet}),
            }
        )
        return f"{base}?{query}"

    return {
        "inferred_scan_url": form_url(result.get("inferred_network")),
        "discover_interfaces": [
            {**iface, "scan_url": form_url(iface.get("suggested_scan"))}
            for iface in result.get("interfaces") or []
        ],
    }


class JobRow:
    """Represents a single job row for UI display."""

    def __init__(
        self,
        job_id: UUID,
        job_type: str,
        agent_id: UUID | None,
        agent_name: str | None,
        status: str,
        priority: int,
        created_at: str,
        started_at: str | None,
        completed_at: str | None,
        duration_seconds: float | None,
        has_result: bool,
        has_error: bool,
        tags: list[str] | None,
        urls: JobUrls,
    ):
        self.job_id = job_id
        # The table shows a truncated id; the slice is display shaping, not the
        # template's job.
        self.short_id = str(job_id)[:8]
        self.urls = urls
        self.job_type = job_type
        self.agent_id = agent_id
        self.agent_name = agent_name or SERVER_RUNNER
        self.status = status
        self.priority = priority
        self.created_at = created_at
        self.started_at = started_at
        self.completed_at = completed_at
        self.duration_seconds = duration_seconds
        self.has_result = has_result
        self.has_error = has_error
        self.tags = tags or []

    @property
    def duration_display(self) -> str:
        """Format duration for display."""
        if self.duration_seconds is None:
            return "-"

        if self.duration_seconds < 1:
            return f"{int(self.duration_seconds * 1000)}ms"
        elif self.duration_seconds < 60:
            return f"{self.duration_seconds:.1f}s"
        elif self.duration_seconds < 3600:
            mins = int(self.duration_seconds // 60)
            secs = int(self.duration_seconds % 60)
            return f"{mins}m {secs}s"
        else:
            hours = int(self.duration_seconds // 3600)
            mins = int((self.duration_seconds % 3600) // 60)
            return f"{hours}h {mins}m"


class JobsViewService:
    """Service for jobs page data aggregation."""

    @staticmethod
    async def list_jobs(
        db: AsyncSession,
        request: Request,
        status: str | None = None,
        job_type: str | None = None,
        agent_filter: str | None = None,
        priority: str | None = None,
        created: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[JobRow], int]:
        """
        Get paginated list of jobs with filtering.

        Args:
            db: Database session
            status: Filter by status
            job_type: Filter by job type
            agent_id: Filter by agent_id
            priority: Filter by priority (high/normal/low)
            created: Filter by created time (1h/24h/7d/30d)
            limit: Max results
            offset: Pagination offset

        Returns:
            Tuple of (job rows, total count)
        """
        # Resolve the runner filter token (uuid | "server" | none) once.
        agent_uuid, server_only = JobCoreService.resolve_runner_filter(agent_filter)

        # Delegate filtering, pagination, and counting to core service
        rows, total = await JobCoreService.list_jobs_with_agents(
            db,
            status=status,
            job_type=job_type,
            agent_id=agent_uuid,
            server_only=server_only,
            priority=priority,
            created=created,
            limit=limit,
            offset=offset,
        )

        # Convert to JobRow — the core already joined the agent name in and
        # derived the flags; what is left here is display shaping.
        job_rows = []
        for row in rows:
            job_row = JobRow(
                job_id=row.id,
                job_type=row.job_type,
                agent_id=row.agent_id,
                agent_name=row.agent_name,
                status=row.status,
                priority=row.priority,
                created_at=row.created_at.isoformat() if row.created_at else "",
                started_at=row.started_at.isoformat() if row.started_at else None,
                completed_at=row.completed_at.isoformat() if row.completed_at else None,
                duration_seconds=row.duration_seconds,
                has_result=row.has_result,
                has_error=row.has_error,
                tags=row.tags,
                urls=build_job_urls(request, row.id),
            )
            job_rows.append(job_row)

        return job_rows, total

    @staticmethod
    async def cancel_job(db: AsyncSession, job_id: UUID) -> None:
        """
        Cancel a pending/running job.

        Args:
            db: Database session
            job_id: Job UUID

        Raises:
            NotFoundException: If job not found or already in terminal state
        """
        if not await JobApiCoreService.cancel_job(db, job_id):
            raise NotFoundException(
                f"Job not found or already in terminal state: {job_id}"
            )
        logger.info("Cancelled job", extra={"job_id": str(job_id)})

    @staticmethod
    def transform_job_form_params(
        form_data: dict[str, Any], job_type: str
    ) -> dict[str, Any]:
        """
        Transform web form data into job params dict based on job schema.

        Uses schema introspection on the registered job class to convert
        form strings to appropriate Python types (int, bool, array, etc.).

        Raises ValueError if job_type is not registered.
        """
        job_class = _JOB_REGISTRY.get(job_type)
        if not job_class:
            raise ValueError(f"Unknown job type: {job_type}")

        params: dict[str, Any] = {}
        if hasattr(job_class, "params_schema"):
            schema_json = job_class.params_schema.model_json_schema()
            properties = schema_json.get("properties", {})

            for field_name, field_info in properties.items():
                field_type = field_info.get("type")
                if field_name in form_data:
                    value = form_data.get(field_name)
                    if field_type == "integer":
                        params[field_name] = int(value) if value else 0
                    elif field_type == "boolean":
                        params[field_name] = value == "on" or value == "true"
                    elif field_type == "array":
                        if value:
                            items_type = field_info.get("items", {}).get("type")
                            if items_type == "integer":
                                params[field_name] = [
                                    int(v.strip())
                                    for v in value.split(",")
                                    if v.strip()
                                ]
                            else:
                                params[field_name] = [
                                    v.strip() for v in value.split(",") if v.strip()
                                ]
                        else:
                            params[field_name] = []
                    else:
                        params[field_name] = value
                elif "default" in field_info:
                    params[field_name] = field_info["default"]

        return params

    @staticmethod
    def build_job_types_metadata() -> dict[str, dict[str, Any]]:
        """
        Build job type metadata for the create-form UI by introspecting
        each registered job class.
        """
        job_types: dict[str, dict[str, Any]] = {}
        for jt, job_class in _JOB_REGISTRY.items():
            schema_json = (
                job_class.params_schema.model_json_schema()
                if hasattr(job_class, "params_schema")
                else {}
            )
            job_types[jt] = {
                "name": jt,
                "display_name": getattr(job_class, "display_name", jt),
                "display_description": getattr(job_class, "display_description", ""),
                "timeout_seconds": getattr(job_class, "default_timeout_seconds", 300),
                "requires_agent": getattr(job_class, "requires_agent", False),
                "schema": schema_json,
                "properties": schema_json.get("properties", {}),
                "required": schema_json.get("required", []),
            }
        return job_types

    @staticmethod
    async def get_merged_form_defaults(
        db: AsyncSession,
        job_type: str | None,
        job_types: dict[str, dict[str, Any]],
        prefill_data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Merge schema defaults < database settings < prefill data.
        """
        schema_defaults: dict[str, Any] = {}
        if job_type and job_type in job_types:
            for field_name, field_info in job_types[job_type]["properties"].items():
                if "default" in field_info:
                    schema_defaults[field_name] = field_info["default"]

        db_settings: dict[str, Any] = {}
        if job_type == "network_scan":
            timeout_setting = await SettingsCoreService.get_setting(
                db, "job.network_scan_timeout", 10
            )
            max_concurrent_setting = await SettingsCoreService.get_setting(
                db, "job.network_scan_max_concurrent", 100
            )
            db_settings = {
                "timeout": timeout_setting,
                "max_concurrent": max_concurrent_setting,
            }
        return {**schema_defaults, **db_settings, **prefill_data}

    @staticmethod
    def parse_tags(tags: str | None) -> list[str] | None:
        """Parse comma-separated tags into a list (None if empty)."""
        if not tags:
            return None
        return [t.strip() for t in tags.split(",") if t.strip()]

    # ------------------------------------------------------------------
    # Page / partial context builders.
    # ------------------------------------------------------------------

    @staticmethod
    async def build_jobs_page_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status: str | None,
        job_type: str | None,
        agent_filter: str | None,
        priority: str | None,
        created: str | None,
        page: int,
        per_page: int | None,
    ) -> dict[str, Any]:
        """Full /jobs page context."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(
                db, "general.default_page_size", 50
            )
        offset = (page - 1) * per_page

        summary = await JobCoreService.get_job_status_summary(db)
        job_rows, total = await JobsViewService.list_jobs(
            db=db,
            request=request,
            status=status,
            job_type=job_type,
            agent_filter=agent_filter,
            priority=priority,
            created=created,
            limit=per_page,
            offset=offset,
        )
        all_agents = await JobCoreService.get_agents_with_jobs(db)
        all_job_types = await JobCoreService.get_distinct_job_types(db)

        filters = {
            "status": status,
            "job_type": job_type,
            "agent_id": agent_filter,
            "priority": priority,
            "created": created,
        }
        pagination = build_pagination(
            page=page, per_page=per_page, total=total, filters=filters
        )

        # Status dropdown options come from the canonical JobStatus enum.
        status_options = [
            {"value": s.value, "label": s.value.replace("_", " ").title()}
            for s in JobStatus
        ]

        return {
            "create_form_url": request.url_for("job_create_form").path,
            # Finished hrefs, resolved by ROUTE NAME. The page used to spell
            # '/jobs/partials/table' and '/jobs' into the template, so moving
            # either route would have left a dead poll and dead pagination links
            # with nothing failing loudly (fw.url_assembly_in_view).
            "table_url": paginated_url(
                request.url_for("jobs_table_partial").path, pagination
            ),
            "page_url": request.url_for("jobs_page").path,
            "request": request,
            "current_user": current_user,
            "summary": summary,
            "job_rows": job_rows,
            "filters": filters,
            "all_agents": all_agents,
            "has_server_jobs": True,
            "all_job_types": all_job_types,
            "status_options": status_options,
            "pagination": pagination,
            "page_title": "Jobs",
        }

    @staticmethod
    async def build_jobs_table_partial_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status: str | None,
        job_type: str | None,
        agent_filter: str | None,
        priority: str | None,
        created: str | None,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        """HTMX partial context for the jobs table (10s polling)."""
        offset = (page - 1) * per_page
        job_rows, total = await JobsViewService.list_jobs(
            db=db,
            request=request,
            status=status,
            job_type=job_type or None,
            agent_filter=agent_filter,
            priority=priority or None,
            created=created or None,
            limit=per_page,
            offset=offset,
        )
        return {
            "request": request,
            "current_user": current_user,
            "job_rows": job_rows,
            "total": total,
        }

    @staticmethod
    async def build_job_detail_context(
        db: AsyncSession, request: Request, current_user: AuthedUser, job_id: UUID
    ) -> dict[str, Any]:
        """
        Job detail panel context. Raises HTTPException(404) if not found.
        Enriches network_scan results, attaches existing-check info, and
        bundles agents + form defaults for the create-checks side panel.
        """
        job = await JobCoreService.get_job_detail(db, job_id)
        if not job:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Job not found: {job_id}",
            )

        agent = None
        if job.agent_id:
            agent = await AgentCoreService.get_agent(db, job.agent_id)

        existing_checks: set[str] = set()
        if job.job_type == "network_scan" and job.result and job.agent_id:
            checks = await CheckCoreService.list_agent_checks(db, job.agent_id)
            existing_checks = {check.display_name for check in checks}

        available_check_tags = await CheckCoreService.get_all_check_tags(db)
        agents = await AgentCoreService.list_agent_options(db)

        check_defaults = await SettingsCoreService.get_check_defaults(db)
        alert_defaults = await SettingsCoreService.get_alert_defaults(db)

        return {
            **build_scan_view(
                job.result if job.job_type == "network_scan" else None, existing_checks
            ),
            "job_urls": build_job_urls(request, job_id),
            "scan_action_urls": build_scan_action_urls(request, job_id),
            **build_discover_view(request, job),
            "request": request,
            "current_user": current_user,
            "job": job,
            "agent": agent,
            "agents": agents,
            "existing_checks": existing_checks,
            "available_check_tags": available_check_tags,
            "check_defaults": check_defaults,
            "alert_defaults": alert_defaults,
        }

    @staticmethod
    async def build_job_create_form_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        job_type: str | None,
        agent_filter: str | None,
        priority: int,
        prefill_params: str | None,
    ) -> dict[str, Any]:
        """Job-creation side-panel form context."""
        agents = await AgentCoreService.list_agent_options(db)

        prefill_data: dict[str, Any] = {}
        if prefill_params:
            try:
                prefill_data = json.loads(prefill_params)
            except json.JSONDecodeError:
                logger.warning(
                    "Invalid prefill_params JSON",
                    extra={"prefill_params": prefill_params},
                )

        job_types = JobsViewService.build_job_types_metadata()
        final_defaults = await JobsViewService.get_merged_form_defaults(
            db, job_type, job_types, prefill_data
        )
        return {
            "request": request,
            "current_user": current_user,
            "agents": agents,
            "job_types": job_types,
            # The form branches on the selected type's metadata; resolving the
            # lookup here keeps the dynamic dict access out of the template.
            "selected_job_type": job_types.get(job_type) if job_type else None,
            "prefill_job_type": job_type,
            "prefill_agent_id": agent_filter,
            "prefill_priority": priority,
            "prefill_params": final_defaults,
            # The form posts to create and re-renders itself when the type
            # changes; both resolved by route name.
            "create_url": str(request.app.url_path_for("create_job_web")),
            "create_form_url": str(request.app.url_path_for("job_create_form")),
        }

    @staticmethod
    def build_error_partial_context(
        request: Request, current_user: AuthedUser, error: str
    ) -> dict[str, Any]:
        """Common error-partial context (matches partials/error_message.html shape)."""
        return {
            "request": request,
            "current_user": current_user,
            "error": error,
        }

    # ------------------------------------------------------------------
    # Mutation orchestrators.
    # Each returns (kind, message, status_code, toast_kind).
    # ------------------------------------------------------------------

    @staticmethod
    async def handle_create_job_form(db: AsyncSession, request: Request) -> str:
        """Orchestrate POST /jobs/create; return the created job_type.

        Raises ValueError (→ 400) on bad input (missing/invalid job_type or
        params); any other failure propagates (→ 500). get_db() owns the
        transaction, so there is no catch-and-rollback here.
        """
        form_data = await request.form()
        job_type = str(form_data.get("job_type", ""))
        agent_id = str(form_data.get("agent_id", ""))
        priority = int(str(form_data.get("priority", 0)))

        if not job_type:
            raise ValueError("Job type is required")

        # transform_job_form_params raises ValueError on invalid params → 400.
        form_dict = {k: str(v) for k, v in form_data.items()}
        params = JobsViewService.transform_job_form_params(form_dict, job_type)

        resolved_agent_id = JobCoreService.resolve_runner_filter(agent_id)[0]
        job_data = JobCreate(
            job_type=JobType(job_type),
            agent_id=resolved_agent_id,
            params=params,
            priority=priority,
        )
        job = await JobApiCoreService.create_job(db, job_data)
        logger.info(
            "Created job via web UI",
            extra={"job_id": str(job.id), "job_type": job_type},
        )
        return job_type

    @staticmethod
    async def handle_delete_job(db: AsyncSession, job_id: UUID) -> None:
        """Delete a job. Raises NotFoundException (→ 404) if it doesn't exist;
        other failures propagate (→ 500). get_db() owns the transaction."""
        if await JobApiCoreService.get_job(db, job_id) is None:
            raise NotFoundException(f"Job not found: {job_id}")
        await JobCoreService.delete_job(db, job_id)
        logger.info(
            "Deleted job via web UI",
            extra={"job_id": str(job_id)},
        )

    @staticmethod
    async def handle_cancel_job(db: AsyncSession, job_id: UUID) -> None:
        """Cancel a job. cancel_job raises NotFoundException (→ 404) if the job
        is missing or already terminal; other failures propagate (→ 500)."""
        await JobsViewService.cancel_job(db, job_id)
        logger.info(
            "Cancelled job via web UI",
            extra={"job_id": str(job_id)},
        )
