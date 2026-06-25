"""
Checks service - provides check data for web UI.

This web service acts as an aggregation layer, delegating to core services
while providing web-specific functionality and data aggregation.
"""

import json
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.check_target_validator import CheckTargetBlockedError
from app.core.exceptions import (
    AgentNotFoundException,
    CheckNotFoundException,
)
from app.core.synthetic_security import SyntheticSecurityError
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.enum_model import CheckType, MaintenanceJobKind, options_for
from app.models.user_model import User
from app.schemas.check_schema import BulkCheckCreateRequest, CheckCreate, CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.web._hx_responses import hx_empty_with_toast
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.services.checks")


# Synthetic checks execute arbitrary Python (Playwright). Admin only.
_SYNTHETIC_ADMIN_DENIED = (
    "Admin access required: Synthetic checks execute arbitrary Python code and "
    "require administrator privileges. Please contact your administrator."
)
_SYNTHETIC_ADMIN_DENIED_UPDATE_SCRIPT = (
    "Admin access required: Updating synthetic check scripts requires "
    "administrator privileges. Please contact your administrator."
)


class CheckRow:
    """Data structure for check display."""

    def __init__(self, check: Check):
        self.id = check.id
        self.agent_name = check.agent.agent_name if check.agent else "unknown"
        self.display_name = check.display_name
        self.check_type = check.check_type
        self.target = check.target
        self.enabled = check.enabled
        self.interval = check.interval_seconds
        self.created_at = check.created_at
        self.updated_at = check.updated_at


class ChecksViewService:
    """Service for checks page data aggregation."""

    @staticmethod
    async def get_checks(
        db: AsyncSession,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        enabled_only: bool = False,
        tag: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Check], int]:
        """
        Get checks with pagination and filtering.
        Excludes internal system checks (agent self-monitoring).

        Args:
            db: Database session
            agent_id: Filter by agent UUID
            check_type: Filter by check type
            enabled_only: Only show enabled checks
            tag: Filter by tag
            search: Search in display_name or target (case-insensitive)
            limit: Max results per page
            offset: Pagination offset

        Returns:
            Tuple of (checks, total count)
        """
        # Get checks, excluding internal system checks
        checks, total = await CheckCoreService.list_checks(
            db=db,
            agent_id=agent_id,
            check_type=check_type,
            enabled_only=enabled_only,
            tag=tag,
            search=search,
            exclude_internal=True,  # Hide system checks from user management
            offset=offset,
            limit=limit,
        )

        return checks, total

    @staticmethod
    async def get_all_agent_ids(db: AsyncSession) -> list[Agent]:
        """
        Get all agents (excluding pending and rejected agents).

        Returns:
            List of Agent objects
        """
        agents, _ = await AgentCoreService.list_agents(db, limit=10000, exclude_pending=True)
        return list(agents)

    @staticmethod
    async def get_all_check_types(db: AsyncSession) -> list[str]:
        """
        Get all unique check types from checks.

        Returns:
            List of check types (ping, http, tcp, json)
        """
        return await CheckCoreService.get_distinct_check_types(db)

    @staticmethod
    async def get_all_tags(db: AsyncSession) -> list[str]:
        """
        Get all unique tags from all checks.

        Returns:
            Sorted list of all unique tags
        """
        return await CheckCoreService.get_all_check_tags(db)

    @staticmethod
    async def get_form_defaults(db: AsyncSession) -> dict:
        """
        Get all defaults needed for check forms.

        Returns:
            Dict with check_defaults and alert_defaults
        """
        check_defaults = await SettingsCoreService.get_check_defaults(db)
        alert_defaults = await SettingsCoreService.get_alert_defaults(db)

        return {
            "check_defaults": check_defaults,
            "alert_defaults": alert_defaults,
        }

    # ====================================================================
    # Web-specific aggregation methods (delegate to core services)
    # ====================================================================

    @staticmethod
    async def get_agents_list(db: AsyncSession):
        """Get all agents for dropdowns (excludes pending)."""
        agents, _ = await AgentCoreService.list_agents(db, limit=1000, exclude_pending=True)
        return agents

    @staticmethod
    async def get_alerts_list(db: AsyncSession):
        """Get all alerts for dropdowns."""
        alerts, _ = await AlertCoreService.list_alerts(db, limit=1000)
        return alerts

    @staticmethod
    async def get_check_by_id(db: AsyncSession, check_id: UUID, include_script_code: bool = False):
        """Get check by ID."""
        return await CheckCoreService.get_check_by_id(
            db, check_id, include_script_code=include_script_code
        )

    @staticmethod
    async def get_alert_ids_for_check(db: AsyncSession, check_id: UUID) -> set[UUID]:
        """Get alert IDs assigned to a check."""
        return await AlertCoreService.get_alert_ids_for_check(db, check_id)

    @staticmethod
    async def build_dependents_panel_context(
        db: AsyncSession,
        request: Request,
        current_user: User,
        parent_check_id: UUID,
        check_type: CheckType | None = None,
        agent_name: str = "",
        tags: str = "",
        search: str = "",
    ) -> dict[str, Any]:
        parent = await CheckCoreService.get_check_by_id(db, parent_check_id)
        candidates = await CheckCoreService.list_potential_dependents(db, parent_check_id)

        search_lc = search.lower().strip()
        type_lc = check_type.lower().strip() if check_type else ""
        agent_lc = agent_name.lower().strip()
        tag_lc = tags.lower().strip()

        filtered = []
        for c in candidates:
            if type_lc and c.check_type.lower() != type_lc:
                continue
            if agent_lc:
                cand_agent = (c.agent.agent_name or "").lower() if c.agent else ""
                if agent_lc != cand_agent:
                    continue
            if tag_lc and (not c.tags or tag_lc not in [t.lower() for t in c.tags]):
                continue
            if search_lc:
                hay = f"{c.display_name} {c.target}".lower()
                if search_lc not in hay:
                    continue
            filtered.append(c)

        filtered.sort(
            key=lambda c: (
                (c.agent.agent_name or "").lower() if c.agent else "",
                c.display_name.lower(),
            )
        )

        current_ids = {c.id for c in candidates if c.depends_on_check_id == parent_check_id}
        all_types = sorted({c.check_type for c in candidates})
        all_agents = sorted(
            {c.agent.agent_name for c in candidates if c.agent and c.agent.agent_name}
        )

        return {
            "request": request,
            "current_user": current_user,
            "parent": parent,
            "candidates": filtered,
            "current_dependent_ids": current_ids,
            "check_type": check_type,
            "agent_name": agent_name,
            "tags": tags,
            "search": search,
            "all_types": all_types,
            "all_agents": all_agents,
        }

    @staticmethod
    async def set_dependents(
        db: AsyncSession,
        parent_check_id: UUID,
        dependent_ids: list[str],
    ) -> tuple[int, int]:
        ids = [UUID(i) for i in dependent_ids if i]
        return await CheckCoreService.set_dependents(db, parent_check_id, ids)

    @staticmethod
    async def get_eligible_parent_checks(
        db: AsyncSession, exclude_check_id: UUID | None = None
    ) -> dict[str, list[Check]]:
        checks = await CheckCoreService.list_eligible_parents(db, exclude_check_id=exclude_check_id)
        grouped: dict[str, list[Check]] = {}
        for c in checks:
            agent_name = c.agent.agent_name if c.agent and c.agent.agent_name else "unknown"
            grouped.setdefault(agent_name, []).append(c)
        for cs in grouped.values():
            cs.sort(key=lambda c: c.display_name.lower())
        return dict(sorted(grouped.items(), key=lambda kv: kv[0].lower()))

    @staticmethod
    async def get_check_create_form_data(db: AsyncSession) -> dict:
        """
        Get all data needed for check create form.

        Returns:
            Dict with agents and alerts lists
        """
        agents = await ChecksViewService.get_agents_list(db)
        alerts = await ChecksViewService.get_alerts_list(db)

        return {
            "agents": agents,
            "alerts": alerts,
        }

    @staticmethod
    async def get_check_edit_form_data(db: AsyncSession, check_id: UUID) -> dict:
        """
        Get all data needed for check edit form.

        Returns:
            Dict with check, agents, alerts, and assigned_alert_ids
        """
        check = await ChecksViewService.get_check_by_id(db, check_id, include_script_code=True)
        agents = await ChecksViewService.get_agents_list(db)
        alerts = await ChecksViewService.get_alerts_list(db)
        assigned_alert_ids = await ChecksViewService.get_alert_ids_for_check(db, check.id)

        return {
            "check": check,
            "agents": agents,
            "alerts": alerts,
            "assigned_alert_ids": assigned_alert_ids,
        }

    @staticmethod
    async def get_check_clone_form_data(db: AsyncSession, check_id: UUID) -> dict:
        """
        Get all data needed for check clone form.

        Returns:
            Dict with source_check, agents, alerts
        """
        source_check = await ChecksViewService.get_check_by_id(db, check_id)
        agents = await ChecksViewService.get_agents_list(db)
        alerts = await ChecksViewService.get_alerts_list(db)

        return {
            "source_check": source_check,
            "agents": agents,
            "alerts": alerts,
        }

    @staticmethod
    async def create_check(
        db: AsyncSession,
        agent_id: UUID,
        check_data: CheckCreate,
        alert_ids: list[UUID],
        *,
        actor_is_admin: bool = False,
    ):
        """View-layer entry point. Workflow lives in CheckCoreService.create_check_with_alerts."""
        return await CheckCoreService.create_check_with_alerts(
            db, agent_id, check_data, alert_ids, actor_is_admin=actor_is_admin
        )

    @staticmethod
    async def update_check(
        db: AsyncSession,
        check_id: UUID,
        update_data: CheckUpdate,
        alert_ids: list[UUID] | None = None,
        *,
        actor_is_admin: bool = False,
    ):
        """View-layer entry point. Workflow lives in CheckCoreService.update_check_with_alerts."""
        return await CheckCoreService.update_check_with_alerts(
            db, check_id, update_data, alert_ids, actor_is_admin=actor_is_admin
        )

    @staticmethod
    async def clone_check(
        db: AsyncSession,
        source_check_id: UUID,
        target_agent_id: UUID,
        overrides: CheckCreate | None,
        alert_ids: list[UUID],
        *,
        actor_is_admin: bool = False,
    ):
        """View-layer entry point. Workflow lives in CheckCoreService.clone_check_with_alerts."""
        return await CheckCoreService.clone_check_with_alerts(
            db,
            source_check_id,
            target_agent_id,
            overrides,
            alert_ids,
            actor_is_admin=actor_is_admin,
        )

    @staticmethod
    async def delete_check(db: AsyncSession, check_id: UUID):
        """Delete check."""
        return await CheckCoreService.delete_check(db, check_id)

    @staticmethod
    async def bulk_create_checks(
        db: AsyncSession, agent_id: UUID, requests: list[BulkCheckCreateRequest]
    ):
        """Bulk create checks."""
        return await CheckCoreService.bulk_create_checks(db, agent_id, requests)

    @staticmethod
    async def bulk_action(db: AsyncSession, check_ids: list[UUID], action: str):
        """Perform bulk action on checks."""
        return await CheckCoreService.bulk_action(db, check_ids, action)

    @staticmethod
    async def bulk_modify(db: AsyncSession, **kwargs):
        """Bulk modify checks (pass-through to core service)."""
        return await CheckCoreService.bulk_modify(db, **kwargs)

    @staticmethod
    async def bulk_preview_checks(url_list: list[str]):
        """Preview checks from URL list."""
        return await CheckCoreService.bulk_preview_checks(url_list)

    @staticmethod
    async def get_setting(db: AsyncSession, key: str, default):
        """Get a setting value."""
        return await SettingsCoreService.get_setting(db, key, default)

    @staticmethod
    async def get_available_agent_tags(db: AsyncSession) -> list[str]:
        """
        Get all available agent tags for assignment selector.

        Returns:
            Sorted list of unique agent tags
        """
        available_tags = set()
        agents, _ = await AgentCoreService.list_agents(db, limit=1000, exclude_pending=True)
        for agent in agents:
            for tag in agent.tags or []:
                tag = tag.strip()
                if tag:
                    available_tags.add(tag)
        return sorted(available_tags)

    @staticmethod
    def get_assignment_mode_text(assignment_mode: str) -> tuple[str, str]:
        """
        Get UI text for assignment mode.

        Args:
            assignment_mode: Assignment mode ("replicate" or "distribute")

        Returns:
            Tuple of (mode_title, mode_description)
        """
        if assignment_mode == "replicate":
            return (
                "Multi-Region Monitoring",
                "This check will run on EVERY agent that matches your selector",
            )
        elif assignment_mode == "distribute":
            return (
                "Load Distribution",
                "Checks will be evenly distributed among matching agents",
            )
        else:
            return ("", "")

    # ------------------------------------------------------------------
    # Form / partial context builders
    # ------------------------------------------------------------------

    @staticmethod
    async def build_assignment_mode_selector_context(
        db: AsyncSession,
        request: Request,
        current_user: User,
        assignment_mode: str,
    ) -> dict[str, Any] | None:
        """HTMX selector partial. Returns None for manual mode (caller renders empty)."""
        if assignment_mode == "manual":
            return None
        available_tags = await ChecksViewService.get_available_agent_tags(db)
        mode_title, mode_description = ChecksViewService.get_assignment_mode_text(assignment_mode)
        return {
            "request": request,
            "current_user": current_user,
            "mode_title": mode_title,
            "mode_description": mode_description,
            "selector_tags": "",
            "available_tags": available_tags,
        }

    @staticmethod
    async def build_create_form_context(
        db: AsyncSession, request: Request, current_user: User
    ) -> dict[str, Any]:
        """Empty 'new check' form context."""
        form_data = await ChecksViewService.get_check_create_form_data(db)
        return {
            "request": request,
            "current_user": current_user,
            "check": None,
            "agents": form_data["agents"],
            "alerts": form_data["alerts"],
            "available_check_tags": await ChecksViewService.get_all_tags(db),
            "available_tags": await ChecksViewService.get_available_agent_tags(db),
            "eligible_parent_checks": await ChecksViewService.get_eligible_parent_checks(db),
            "check_type_options": options_for(CheckType),
            **(await ChecksViewService.get_form_defaults(db)),
        }

    @staticmethod
    async def build_edit_form_context(
        db: AsyncSession, request: Request, current_user: User, check_id: UUID
    ) -> dict[str, Any]:
        """Populated edit form context (raises CheckNotFoundException)."""
        form_data = await ChecksViewService.get_check_edit_form_data(db, check_id)
        check = form_data["check"]
        mode_title, mode_description = ChecksViewService.get_assignment_mode_text(
            check.assignment_mode
        )
        return {
            "request": request,
            "current_user": current_user,
            "check": check,
            "agents": form_data["agents"],
            "alerts": form_data["alerts"],
            "assigned_alert_ids": form_data["assigned_alert_ids"],
            "available_check_tags": await ChecksViewService.get_all_tags(db),
            "available_tags": await ChecksViewService.get_available_agent_tags(db),
            "eligible_parent_checks": await ChecksViewService.get_eligible_parent_checks(
                db, exclude_check_id=check.id
            ),
            "mode_title": mode_title,
            "mode_description": mode_description,
            "check_type_options": options_for(CheckType),
            **(await ChecksViewService.get_form_defaults(db)),
        }

    @staticmethod
    async def build_clone_form_context(
        db: AsyncSession, request: Request, current_user: User, check_id: UUID
    ) -> dict[str, Any]:
        """Populated clone form context (raises CheckNotFoundException)."""
        form_data = await ChecksViewService.get_check_clone_form_data(db, check_id)
        check = form_data["source_check"]
        return {
            "request": request,
            "current_user": current_user,
            "check": check,
            "agents": form_data["agents"],
            "alerts": form_data["alerts"],
            "assigned_alert_ids": await ChecksViewService.get_alert_ids_for_check(db, check.id),
            "available_check_tags": await ChecksViewService.get_all_tags(db),
            "available_tags": await ChecksViewService.get_available_agent_tags(db),
            "eligible_parent_checks": await ChecksViewService.get_eligible_parent_checks(
                db, exclude_check_id=check.id
            ),
            "mode": "clone",
            "mode_title": "Clone Check",
            "mode_description": f"Cloning from: {check.display_name} — All fields editable",
            "suggested_name": f"{check.display_name}-clone",
            "check_type_options": options_for(CheckType),
            **(await ChecksViewService.get_form_defaults(db)),
        }

    @staticmethod
    def build_error_partial_context(
        request: Request, current_user: User, error: str
    ) -> dict[str, Any]:
        """Common error-partial context."""
        return {
            "request": request,
            "current_user": current_user,
            "error": error,
        }

    # ------------------------------------------------------------------
    # Rendered responses — the view owns presentation; routers just return
    # these. (LUXSWIRL-163/172: no render helpers or template selection in
    # the router.)
    # ------------------------------------------------------------------

    @staticmethod
    def error_response(
        request: Request, current_user: User, error: str, status_code: int
    ) -> Response:
        """Render the error-message partial as a response."""
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            ChecksViewService.build_error_partial_context(request, current_user, error),
            status_code=status_code,
        )

    @staticmethod
    def close_panel_response() -> Response:
        """'Mutation succeeded — close panel + refresh page' HTMX response."""
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "closeSidePanel,refreshPage"},
        )

    @staticmethod
    def job_status_response(request: Request, current_user: User, job: Any) -> Response:
        """Render the maintenance-job polling partial for the side panel."""
        return templates.TemplateResponse(
            request,
            "partials/maintenance/job_status.html",
            {"job": job, "request": request, "current_user": current_user},
        )

    @staticmethod
    def build_toggle_button_context(check_id: UUID, enabled: bool) -> dict[str, Any]:
        """Context for the toggle-button partial."""
        return {"check_id": check_id, "enabled": enabled}

    @staticmethod
    def assert_synthetic_admin_create(check_type: str, current_user: User) -> str | None:
        """Return error message if user can't create a synthetic check, else None."""
        if check_type == "synthetic" and current_user.role != "admin":
            logger.warning(
                "User attempted to create synthetic check without admin privileges",
                extra={
                    "username": current_user.username,
                    "role": current_user.role,
                },
            )
            return _SYNTHETIC_ADMIN_DENIED
        return None

    @staticmethod
    def assert_synthetic_admin_update(
        new_check_type: str,
        existing_check: Check,
        script_code: str | None,
        current_user: User,
    ) -> str | None:
        """Return error message if user can't update to/within a synthetic check."""
        if new_check_type == "synthetic" and current_user.role != "admin":
            logger.warning(
                "User attempted to update to synthetic check without admin privileges",
                extra={
                    "username": current_user.username,
                    "role": current_user.role,
                },
            )
            return _SYNTHETIC_ADMIN_DENIED
        if (
            existing_check.check_type == "synthetic"
            and script_code
            and current_user.role != "admin"
        ):
            logger.warning(
                "User attempted to update synthetic check script without admin privileges",
                extra={
                    "username": current_user.username,
                    "role": current_user.role,
                },
            )
            return _SYNTHETIC_ADMIN_DENIED_UPDATE_SCRIPT
        return None

    # ------------------------------------------------------------------
    # Mutation orchestrators (full form → schema → core).
    # Router calls one of these per endpoint; no further logic in router.
    # Each returns (kind, error_message, http_status).
    #   kind ∈ {"ok", "error"}
    #   error_message is None on success
    #   http_status is the suggested response status
    # ------------------------------------------------------------------

    @staticmethod
    async def handle_create_check_form(
        db: AsyncSession,
        request: Request,
        current_user: User,
        form_kwargs: dict[str, Any],
    ) -> Response:
        """
        Orchestrate POST /checks/create. Branches into bulk import or single
        check create based on `check_type`. Synthetic checks require admin.
        """
        check_type = form_kwargs["check_type"]
        denial = ChecksViewService.assert_synthetic_admin_create(check_type, current_user)
        if denial is not None:
            return ChecksViewService.error_response(request, current_user, denial, 403)

        try:
            if check_type == "http-bulk":
                form_data = await request.form()
                bulk_item_urls = [str(i) for i in form_data.getlist("bulk_item_url")]
                bulk_item_names = [str(i) for i in form_data.getlist("bulk_item_name")]
                if not bulk_item_urls:
                    return ChecksViewService.error_response(
                        request, current_user, "No URLs provided for bulk import", 400
                    )
                bulk_requests = ChecksViewService.build_bulk_requests(
                    bulk_item_urls,
                    bulk_item_names,
                    form_kwargs["interval"],
                    form_kwargs["enabled"],
                    form_kwargs.get("tags", ""),
                    form_kwargs.get("expected_status"),
                    form_kwargs.get("http_method"),
                    form_kwargs.get("verify_ssl", True),
                    form_kwargs.get("timeout_seconds", ""),
                    form_kwargs.get("retry_attempts", ""),
                    form_kwargs.get("retry_interval_seconds", ""),
                    form_kwargs.get("resend_notification_after", ""),
                )
                # Enqueue background job — bulk URL creates can be 100+ inserts;
                # web route commits the intent + returns polling partial. See LUXSWIRL-105.
                job = await MaintenanceJobCoreService.enqueue(
                    db,
                    kind=MaintenanceJobKind.BULK_CHECK_CREATE,
                    target_id=form_kwargs["agent_id"],
                    params={
                        "agent_id": str(form_kwargs["agent_id"]),
                        "requests": [r.model_dump(mode="json") for r in bulk_requests],
                        "alert_ids": [str(a) for a in form_kwargs.get("alert_ids", [])],
                    },
                    owner_id=current_user.id,
                )
                logger.info(
                    "Enqueued bulk_check_create maintenance job",
                    extra={
                        "agent_id": str(form_kwargs["agent_id"]),
                        "count": len(bulk_requests),
                        "job_id": str(job.id),
                    },
                )
                return ChecksViewService.job_status_response(request, current_user, job)

            # Single check
            _agent, resolved_id = await AgentCoreService.resolve_for_assignment(
                db, form_kwargs["agent_id"], form_kwargs["assignment_mode"]
            )
            check_data = ChecksViewService.transform_form_data(
                display_name=form_kwargs["display_name"],
                check_type=check_type,
                target=form_kwargs.get("target", ""),
                enabled=form_kwargs["enabled"],
                interval=form_kwargs["interval"],
                tags=form_kwargs.get("tags"),
                http_method=form_kwargs.get("http_method"),
                expected_status=form_kwargs.get("expected_status"),
                json_path=form_kwargs.get("json_path"),
                expected_value=form_kwargs.get("expected_value"),
                script_code=form_kwargs.get("script_code"),
                record_type=form_kwargs.get("record_type"),
                nameserver=form_kwargs.get("nameserver"),
                port=form_kwargs.get("port"),
                expect_value=form_kwargs.get("expect_value"),
                connection_string=form_kwargs.get("connection_string"),
                query=form_kwargs.get("query"),
                assignment_mode=form_kwargs["assignment_mode"],
                agent_selector=form_kwargs.get("agent_selector"),
                description=form_kwargs.get("description", ""),
                timeout_seconds=form_kwargs.get("timeout_seconds", ""),
                verify_ssl=form_kwargs.get("verify_ssl", True),
                retry_attempts=form_kwargs.get("retry_attempts", ""),
                retry_interval_seconds=form_kwargs.get("retry_interval_seconds", ""),
                resend_notification_after=form_kwargs.get("resend_notification_after", ""),
                depends_on_check_id=form_kwargs.get("depends_on_check_id"),
            )
            await ChecksViewService.create_check(
                db,
                resolved_id,
                check_data,
                form_kwargs.get("alert_ids", []),
                actor_is_admin=current_user.role == "admin",
            )
            logger.info(
                "Created check with alert assignments",
                extra={
                    "agent_id": str(resolved_id),
                    "display_name": form_kwargs["display_name"],
                    "alert_count": len(form_kwargs.get("alert_ids", [])),
                },
            )
            return ChecksViewService.close_panel_response()

        except SyntheticSecurityError as e:
            logger.error("Synthetic check security validation failed", exc_info=True)
            return ChecksViewService.error_response(
                request, current_user, f"Security Validation Failed\n\n{e}", 400
            )
        except CheckTargetBlockedError as e:
            logger.warning("Check target blocked by SSRF protection", exc_info=True)
            return ChecksViewService.error_response(
                request, current_user, f"Target Blocked\n\n{e}", 400
            )
        except AgentNotFoundException as e:
            return ChecksViewService.error_response(request, current_user, str(e), 404)

    @staticmethod
    async def handle_update_check_form(
        db: AsyncSession,
        request: Request,
        check_id: UUID,
        current_user: User,
        form_kwargs: dict[str, Any],
    ) -> Response:
        """Orchestrate POST /checks/{id}/update. Synthetic admin gate, transform, update."""
        try:
            check = await ChecksViewService.get_check_by_id(db, check_id)
            denial = ChecksViewService.assert_synthetic_admin_update(
                form_kwargs["check_type"],
                check,
                form_kwargs.get("script_code"),
                current_user,
            )
            if denial is not None:
                return ChecksViewService.error_response(request, current_user, denial, 403)

            update_data = ChecksViewService.transform_form_data_for_update(
                display_name=(
                    form_kwargs["display_name"].strip() if form_kwargs.get("display_name") else None
                ),
                target=form_kwargs.get("target"),
                enabled=form_kwargs["enabled"],
                interval=form_kwargs["interval"],
                tags=form_kwargs.get("tags"),
                http_method=form_kwargs.get("http_method"),
                expected_status=form_kwargs.get("expected_status"),
                json_path=form_kwargs.get("json_path"),
                expected_value=form_kwargs.get("expected_value"),
                script_code=form_kwargs.get("script_code"),
                record_type=form_kwargs.get("record_type"),
                nameserver=form_kwargs.get("nameserver"),
                port=form_kwargs.get("port"),
                expect_value=form_kwargs.get("expect_value"),
                connection_string=form_kwargs.get("connection_string"),
                query=form_kwargs.get("query"),
                assignment_mode=form_kwargs.get("assignment_mode"),
                agent_selector=form_kwargs.get("agent_selector"),
                description=form_kwargs.get("description", ""),
                timeout_seconds=form_kwargs.get("timeout_seconds", ""),
                verify_ssl=form_kwargs.get("verify_ssl"),
                retry_attempts=form_kwargs.get("retry_attempts", ""),
                retry_interval_seconds=form_kwargs.get("retry_interval_seconds", ""),
                resend_notification_after=form_kwargs.get("resend_notification_after", ""),
                depends_on_check_id=form_kwargs.get("depends_on_check_id"),
            )
            await ChecksViewService.update_check(
                db,
                check.id,
                update_data,
                form_kwargs.get("alert_ids", []),
                actor_is_admin=current_user.role == "admin",
            )
            logger.info(
                "Updated check with alert assignments",
                extra={
                    "agent_id": str(check.agent.id),
                    "display_name": check.display_name,
                    "alert_count": len(form_kwargs.get("alert_ids", [])),
                },
            )
            return ChecksViewService.close_panel_response()

        except CheckNotFoundException as e:
            logger.error("Check not found", exc_info=True)
            return ChecksViewService.error_response(request, current_user, str(e), 404)
        except SyntheticSecurityError as e:
            logger.error("Synthetic check security validation failed", exc_info=True)
            return ChecksViewService.error_response(
                request, current_user, f"Security Validation Failed\n\n{e}", 400
            )
        except CheckTargetBlockedError as e:
            logger.warning("Check target blocked by SSRF protection", exc_info=True)
            return ChecksViewService.error_response(
                request, current_user, f"Target Blocked\n\n{e}", 400
            )

    @staticmethod
    async def handle_clone_check_form(
        db: AsyncSession,
        request: Request,
        source_check_id: UUID,
        current_user: User,
        form_kwargs: dict[str, Any],
    ) -> Response:
        """Orchestrate POST /checks/{id}/clone."""
        try:
            source_check = await ChecksViewService.get_check_by_id(db, source_check_id)

            try:
                target_agent, resolved_id = await AgentCoreService.resolve_for_assignment(
                    db, form_kwargs["agent_id"], form_kwargs["assignment_mode"]
                )
            except AgentNotFoundException as e:
                return ChecksViewService.error_response(request, current_user, str(e), 404)

            display_name = form_kwargs["display_name"]
            if not display_name or not display_name.strip():
                return ChecksViewService.error_response(
                    request, current_user, "Display name is required", 400
                )

            logger.info(
                "Clone form received script_code",
                extra={"script_code_length": len(form_kwargs.get("script_code") or "")},
            )
            overrides = ChecksViewService.transform_form_data(
                display_name=display_name.strip(),
                check_type=form_kwargs["check_type"],
                target=form_kwargs["target"],
                enabled=form_kwargs["enabled"],
                interval=form_kwargs["interval"],
                tags=form_kwargs.get("tags"),
                http_method=form_kwargs.get("http_method"),
                expected_status=form_kwargs.get("expected_status"),
                json_path=form_kwargs.get("json_path"),
                expected_value=form_kwargs.get("expected_value"),
                script_code=form_kwargs.get("script_code"),
                assignment_mode=form_kwargs["assignment_mode"],
                agent_selector=form_kwargs.get("agent_selector"),
                description=form_kwargs.get("description", ""),
                timeout_seconds=form_kwargs.get("timeout_seconds", ""),
                verify_ssl=form_kwargs.get("verify_ssl", True),
                retry_attempts=form_kwargs.get("retry_attempts", ""),
                retry_interval_seconds=form_kwargs.get("retry_interval_seconds", ""),
                resend_notification_after=form_kwargs.get("resend_notification_after", ""),
                depends_on_check_id=form_kwargs.get("depends_on_check_id"),
            )
            cloned_check = await ChecksViewService.clone_check(
                db,
                source_check_id=source_check_id,
                target_agent_id=resolved_id,
                overrides=overrides,
                alert_ids=form_kwargs.get("alert_ids", []),
                actor_is_admin=current_user.role == "admin",
            )
            logger.info(
                "Cloned check",
                extra={
                    "source_check_name": source_check.fully_qualified_name,
                    "target_agent_name": target_agent.agent_name,
                    "cloned_check_name": cloned_check.display_name,
                    "cloned_check_id": str(cloned_check.id),
                },
            )
            return ChecksViewService.close_panel_response()

        except CheckNotFoundException as e:
            logger.error("Check not found", exc_info=True)
            return ChecksViewService.error_response(request, current_user, str(e), 404)

    @staticmethod
    async def toggle_check_handler(db: AsyncSession, check_id: UUID) -> dict[str, Any]:
        """Toggle enabled flag, return context for the toggle-button partial."""
        check = await ChecksViewService.get_check_by_id(db, check_id)
        updated_check = await ChecksViewService.update_check(
            db, check.id, CheckUpdate(enabled=not check.enabled)
        )
        logger.info(
            "Toggled check",
            extra={
                "agent_id": str(check.agent.id),
                "display_name": check.display_name,
                "enabled": updated_check.enabled,
            },
        )
        return ChecksViewService.build_toggle_button_context(check_id, updated_check.enabled)

    @staticmethod
    async def enqueue_check_delete(
        db: AsyncSession, request: Request, current_user: User, check_id: UUID
    ) -> Response:
        """Enqueue a background delete for a single check, returning the response.

        A check delete cascades all of its check_results — a TimescaleDB
        hypertable, potentially millions of rows — so the cascade runs in the
        maintenance worker, not the web request (mirrors the bulk path).
        See LUXSWIRL-105.
        """
        try:
            check = await ChecksViewService.get_check_by_id(db, check_id)
            job = await MaintenanceJobCoreService.enqueue(
                db,
                kind=MaintenanceJobKind.BULK_CHECK_DELETE,
                target_id=None,
                params={"check_ids": [str(check.id)]},
                owner_id=current_user.id,
            )
            logger.info(
                "Enqueued single check delete",
                extra={"check_id": str(check.id), "job_id": str(job.id)},
            )
            return hx_empty_with_toast(
                "Check deletion queued",
                extra_events={"closeSidePanel": {}, "refreshPage": {}},
            )
        except CheckNotFoundException as e:
            logger.error("Check not found", exc_info=True)
            return ChecksViewService.error_response(request, current_user, str(e), 404)

    # ------------------------------------------------------------------
    # Bulk operations + their OOB-swap context builders
    # ------------------------------------------------------------------

    @staticmethod
    async def _resolve_bulk_check_ids(
        db: AsyncSession,
        select_all: str,
        agent: UUID | None,
        enabled: str,
        tag: str,
        check_ids: list[UUID],
        list_limit: int,
    ) -> list[UUID]:
        """If select_all=='true', expand current filters to UUIDs; else parse."""
        if select_all == "true":
            checks, _ = await ChecksViewService.get_checks(
                db=db,
                agent_id=agent,
                enabled_only=(enabled == "true") if enabled != "all" else False,
                tag=tag or None,
                limit=list_limit,
                offset=0,
            )
            return [check.id for check in checks]
        return list(check_ids)

    @staticmethod
    async def enqueue_bulk_delete(
        db: AsyncSession,
        select_all: str,
        agent: UUID | None,
        enabled: str,
        tag: str,
        check_ids: list[UUID],
        owner_id: UUID | None = None,
    ):
        """Resolve the bulk selection then enqueue a bulk_check_delete job.

        Returns (MaintenanceJob, resolved_count). See LUXSWIRL-105.
        """
        ids = await ChecksViewService._resolve_bulk_check_ids(
            db, select_all, agent, enabled, tag, check_ids, list_limit=100000
        )
        job = await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.BULK_CHECK_DELETE,
            target_id=None,
            params={"check_ids": [str(c) for c in ids]},
            owner_id=owner_id,
        )
        return job, len(ids)

    @staticmethod
    async def enqueue_bulk_check_action(
        db: AsyncSession,
        action: str,
        select_all: str,
        agent: UUID | None,
        enabled: str,
        tag: str,
        check_ids: list[UUID],
        owner_id: UUID | None = None,
    ):
        """Enqueue a maintenance job for a bulk check action (delete/enable/disable).

        Dispatches to either bulk_check_delete or bulk_check_toggle depending on
        the action. Returns (job, resolved_count, toast_message).
        """
        ids = await ChecksViewService._resolve_bulk_check_ids(
            db, select_all, agent, enabled, tag, check_ids, list_limit=100000
        )
        if action == "delete":
            job = await MaintenanceJobCoreService.enqueue(
                db,
                kind=MaintenanceJobKind.BULK_CHECK_DELETE,
                target_id=None,
                params={"check_ids": [str(c) for c in ids]},
                owner_id=owner_id,
            )
            msg = f"Deleting {len(ids)} check(s) in background…"
        elif action in ("enable", "disable"):
            job = await MaintenanceJobCoreService.enqueue(
                db,
                kind=MaintenanceJobKind.BULK_CHECK_TOGGLE,
                target_id=None,
                params={"action": action, "check_ids": [str(c) for c in ids]},
                owner_id=owner_id,
            )
            verb = "Enabling" if action == "enable" else "Disabling"
            msg = f"{verb} {len(ids)} check(s) in background…"
        else:
            raise ValueError(f"Unsupported bulk action: {action!r}")
        return job, len(ids), msg

    @staticmethod
    async def enqueue_bulk_modify(
        db: AsyncSession,
        select_all: str,
        agent: UUID | None,
        enabled: str,
        tag: str,
        check_ids: list[UUID],
        update_data,
        new_agent_id: UUID | None,
        alert_id: UUID | None,
        owner_id: UUID | None = None,
    ):
        """Resolve selection + enqueue bulk_check_modify job.

        Returns (MaintenanceJob, resolved_count). See LUXSWIRL-105.
        """
        ids = await ChecksViewService._resolve_bulk_check_ids(
            db, select_all, agent, enabled, tag, check_ids, list_limit=10000
        )
        job = await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.BULK_CHECK_MODIFY,
            target_id=None,
            params={
                "check_ids": [str(c) for c in ids],
                "update_fields": update_data.model_dump(exclude_unset=True),
                "new_agent_id": str(new_agent_id) if new_agent_id else None,
                "alert_id": str(alert_id) if alert_id else None,
            },
            owner_id=owner_id,
        )
        return job, len(ids)

    @staticmethod
    async def execute_bulk_action(
        db: AsyncSession,
        action: str,
        select_all: str,
        agent: UUID | None,
        enabled: str,
        tag: str,
        check_ids: list[UUID],
    ) -> tuple[int, int, str, str]:
        """
        Run a bulk enable/disable/delete and return
        (success_count, failure_count, message, toast_kind).

        toast_kind ∈ {"success", "warning"} — warning if any failures.
        Router calls StatusViewService separately to build the OOB-swap context.
        """
        check_ids_to_process = await ChecksViewService._resolve_bulk_check_ids(
            db, select_all, agent, enabled, tag, check_ids, list_limit=100000
        )
        logger.info(
            "Bulk action requested",
            extra={"action": action, "check_count": len(check_ids_to_process)},
        )

        result = await ChecksViewService.bulk_action(db, check_ids_to_process, action)
        logger.info(
            "Bulk action complete",
            extra={
                "success_count": result["success_count"],
                "failure_count": result["failure_count"],
            },
        )

        action_past = {"enable": "Enabled", "disable": "Disabled", "delete": "Deleted"}.get(
            action, action
        )
        count = result["success_count"]
        message = f"{action_past} {count} check{'s' if count != 1 else ''}"
        if result["failure_count"] > 0:
            message += f" ({result['failure_count']} failed)"
        toast_kind = "success" if result["failure_count"] == 0 else "warning"
        return result["success_count"], result["failure_count"], message, toast_kind

    @staticmethod
    async def build_bulk_preview_context(
        db: AsyncSession,
        request: Request,
        current_user: User,
        bulk_urls: str,
    ) -> dict[str, Any] | None:
        """Bulk preview partial context. Returns None if no URLs given."""
        url_list = [url.strip() for url in bulk_urls.split("\n") if url.strip()]
        if not url_list:
            return None
        previews = await ChecksViewService.bulk_preview_checks(url_list)
        return {
            "request": request,
            "current_user": current_user,
            "previews": previews,
        }

    @staticmethod
    async def build_table_partial_context(
        db: AsyncSession,
        request: Request,
        current_user: User,
        agent: UUID | None,
        enabled: str,
        page: int,
        per_page: int | None,
    ) -> dict[str, Any]:
        """Checks-table partial context."""
        if per_page is None:
            per_page = await ChecksViewService.get_setting(db, "general.default_page_size", 50)
        offset = (page - 1) * per_page
        enabled_only = enabled == "true"

        assignments, total = await ChecksViewService.get_checks(
            db=db,
            agent_id=agent,
            enabled_only=enabled_only if enabled != "all" else False,
            limit=per_page,
            offset=offset,
        )
        return {
            "request": request,
            "current_user": current_user,
            "assignments": assignments,
            "total": total,
            "page": page,
            "per_page": per_page,
            "filters": {"agent": agent, "enabled": enabled},
        }

    @staticmethod
    def parse_tags(tags: str | None) -> list[str] | None:
        """
        Parse comma-separated tags string into list.

        Args:
            tags: Comma-separated tags string

        Returns:
            List of trimmed tags or None if empty
        """
        if not tags:
            return None
        return [t.strip() for t in tags.split(",") if t.strip()]

    @staticmethod
    def parse_optional_int(value: str | None) -> int | None:
        """
        Parse optional integer from form string.

        Args:
            value: String value from form (may be empty)

        Returns:
            Parsed integer or None if empty/invalid
        """
        if not value:
            return None
        try:
            return int(value)
        except ValueError, TypeError:
            return None

    @staticmethod
    def parse_agent_selector(agent_selector: str | None) -> dict | None:
        """
        Parse agent selector JSON string.

        Args:
            agent_selector: JSON string from form

        Returns:
            Parsed dict or None if empty/invalid
        """
        if not agent_selector:
            return None
        try:
            return cast(dict[str, str] | None, json.loads(agent_selector))
        except json.JSONDecodeError:
            logger.warning(
                "Invalid agent_selector JSON",
                extra={"agent_selector": agent_selector},
            )
            return None

    @staticmethod
    def transform_form_data(
        display_name: str,
        check_type: str,
        target: str,
        enabled: bool,
        interval: int,
        tags: str | None = None,
        # HTTP fields
        http_method: str | None = None,
        expected_status: int | None = None,
        # JSON fields
        json_path: str | None = None,
        expected_value: str | None = None,
        # Synthetic fields
        script_code: str | None = None,
        # DNS fields
        record_type: str | None = None,
        nameserver: str | None = None,
        port: str | None = None,
        expect_value: str | None = None,
        # Database fields
        connection_string: str | None = None,
        query: str | None = None,
        # Assignment fields
        assignment_mode: str = "manual",
        agent_selector: str | None = None,
        # Advanced settings
        description: str | None = None,
        timeout_seconds: str | None = None,
        verify_ssl: bool = True,
        retry_attempts: str | None = None,
        retry_interval_seconds: str | None = None,
        resend_notification_after: str | None = None,
        depends_on_check_id: UUID | None = None,
    ) -> CheckCreate:
        """
        Transform web form data into CheckCreate schema.

        Handles all parsing, type conversion, and validation of form data.
        This eliminates duplicated transformation logic across endpoints.

        Args:
            display_name: Check display name
            check_type: Check type (http, dns, mysql, etc.)
            target: Check target (URL, hostname, etc.)
            enabled: Whether check is enabled
            interval: Check interval in seconds
            tags: Comma-separated tags
            ... (all other form fields)

        Returns:
            CheckCreate schema ready for service layer
        """
        # Parse complex fields
        tags_list = ChecksViewService.parse_tags(tags)
        agent_selector_dict = ChecksViewService.parse_agent_selector(agent_selector)

        # Parse optional integers
        timeout_seconds_int = ChecksViewService.parse_optional_int(timeout_seconds)
        retry_attempts_int = ChecksViewService.parse_optional_int(retry_attempts)
        retry_interval_seconds_int = ChecksViewService.parse_optional_int(retry_interval_seconds)
        resend_notification_after_int = ChecksViewService.parse_optional_int(
            resend_notification_after
        )
        port_int = ChecksViewService.parse_optional_int(port)

        # Build kwargs - only include retry_interval_seconds if provided
        # (it has a default in schema, passing None breaks Pydantic validation)
        kwargs: dict[str, Any] = {
            "display_name": display_name,
            "check_type": check_type,
            "target": target,
            "enabled": enabled,
            "interval_seconds": interval,
            "http_method": http_method,
            "expected_status": expected_status,
            "json_path": json_path,
            "expected_value": expected_value,
            "script_code": script_code if script_code else None,
            "tags": tags_list,
            "assignment_mode": assignment_mode,
            "agent_selector": agent_selector_dict,
            # DNS-specific fields
            "record_type": record_type,
            "nameserver": nameserver,
            "port": port_int,
            "expect_value": expect_value,
            # Database check fields (MySQL/Postgres)
            "connection_string": connection_string,
            "query": query,
            # Advanced settings
            "description": description if description else None,
            "timeout_seconds": timeout_seconds_int,
            "verify_ssl": verify_ssl,
            "retry_attempts": retry_attempts_int,
            "resend_notification_after": resend_notification_after_int,
            "depends_on_check_id": depends_on_check_id,
        }

        # Only pass retry_interval_seconds if not None (has schema default of 30)
        if retry_interval_seconds_int is not None:
            kwargs["retry_interval_seconds"] = retry_interval_seconds_int

        return CheckCreate(**kwargs)

    @staticmethod
    def transform_form_data_for_update(
        display_name: str | None = None,
        target: str | None = None,
        enabled: bool | None = None,
        interval: int | None = None,
        tags: str | None = None,
        # HTTP fields
        http_method: str | None = None,
        expected_status: int | None = None,
        # JSON fields
        json_path: str | None = None,
        expected_value: str | None = None,
        # Synthetic fields
        script_code: str | None = None,
        # DNS fields
        record_type: str | None = None,
        nameserver: str | None = None,
        port: str | None = None,
        expect_value: str | None = None,
        # Database fields
        connection_string: str | None = None,
        query: str | None = None,
        # Assignment fields
        assignment_mode: str | None = None,
        agent_selector: str | None = None,
        # Advanced settings
        description: str | None = None,
        timeout_seconds: str | None = None,
        verify_ssl: bool | None = None,
        retry_attempts: str | None = None,
        retry_interval_seconds: str | None = None,
        resend_notification_after: str | None = None,
        depends_on_check_id: UUID | None = None,
    ) -> CheckUpdate:
        """
        Transform web form data into CheckUpdate schema.

        Similar to transform_form_data but for updates (all fields optional).

        Args:
            ... (all form fields, all optional)

        Returns:
            CheckUpdate schema ready for service layer
        """
        # Parse complex fields if provided
        tags_list = ChecksViewService.parse_tags(tags) if tags is not None else None
        agent_selector_dict = (
            ChecksViewService.parse_agent_selector(agent_selector)
            if agent_selector is not None
            else None
        )

        # Parse optional integers
        timeout_seconds_int = ChecksViewService.parse_optional_int(timeout_seconds)
        retry_attempts_int = ChecksViewService.parse_optional_int(retry_attempts)
        retry_interval_seconds_int = ChecksViewService.parse_optional_int(retry_interval_seconds)
        resend_notification_after_int = ChecksViewService.parse_optional_int(
            resend_notification_after
        )
        port_int = ChecksViewService.parse_optional_int(port)

        # Build dict with only provided fields
        update_data: dict[str, Any] = {}
        if display_name is not None:
            update_data["display_name"] = display_name
        if target is not None:
            update_data["target"] = target
        if enabled is not None:
            update_data["enabled"] = enabled
        if interval is not None:
            update_data["interval_seconds"] = interval
        if http_method is not None:
            update_data["http_method"] = http_method
        if expected_status is not None:
            update_data["expected_status"] = expected_status
        if json_path is not None:
            update_data["json_path"] = json_path
        if expected_value is not None:
            update_data["expected_value"] = expected_value
        if script_code is not None:
            update_data["script_code"] = script_code if script_code else None
        if tags_list is not None:
            update_data["tags"] = tags_list
        if assignment_mode is not None:
            update_data["assignment_mode"] = assignment_mode
        if agent_selector_dict is not None:
            update_data["agent_selector"] = agent_selector_dict
        if record_type is not None:
            update_data["record_type"] = record_type
        if nameserver is not None:
            update_data["nameserver"] = nameserver
        if port_int is not None:
            update_data["port"] = port_int
        if expect_value is not None:
            update_data["expect_value"] = expect_value
        if connection_string is not None:
            update_data["connection_string"] = connection_string
        if query is not None:
            update_data["query"] = query
        if description is not None:
            update_data["description"] = description if description else None
        if timeout_seconds_int is not None:
            update_data["timeout_seconds"] = timeout_seconds_int
        if verify_ssl is not None:
            update_data["verify_ssl"] = verify_ssl
        if retry_attempts_int is not None:
            update_data["retry_attempts"] = retry_attempts_int
        if retry_interval_seconds_int is not None:
            update_data["retry_interval_seconds"] = retry_interval_seconds_int
        if resend_notification_after_int is not None:
            update_data["resend_notification_after"] = resend_notification_after_int
        if depends_on_check_id is not None:
            update_data["depends_on_check_id"] = depends_on_check_id

        return CheckUpdate(**update_data)

    @staticmethod
    def build_bulk_requests(
        bulk_item_urls: list[str],
        bulk_item_names: list[str],
        interval: int,
        enabled: bool,
        tags: str | None = None,
        expected_status: int | None = None,
        http_method: str | None = None,
        verify_ssl: bool = True,
        timeout_seconds: str | None = None,
        retry_attempts: str | None = None,
        retry_interval_seconds: str | None = None,
        resend_notification_after: str | None = None,
    ) -> list[BulkCheckCreateRequest]:
        """
        Build bulk check create requests from form data.

        Args:
            bulk_item_urls: List of URLs from form
            bulk_item_names: List of display names from form
            interval: Check interval in seconds
            enabled: Whether checks are enabled
            tags: Comma-separated tags
            ... (other form fields)

        Returns:
            List of BulkCheckCreateRequest objects
        """
        # Parse shared fields
        tags_list = ChecksViewService.parse_tags(tags)
        timeout_seconds_int = ChecksViewService.parse_optional_int(timeout_seconds) or 10
        ChecksViewService.parse_optional_int(retry_attempts)
        ChecksViewService.parse_optional_int(retry_interval_seconds)
        ChecksViewService.parse_optional_int(resend_notification_after)

        # Build requests
        bulk_requests = []
        for url, display_name in zip(bulk_item_urls, bulk_item_names, strict=False):
            req = BulkCheckCreateRequest(
                url=url,
                display_name=display_name.strip() if display_name else None,
                interval_seconds=interval,
                timeout_seconds=timeout_seconds_int,
                enabled=enabled,
                tags=tags_list,
                expected_status=expected_status if expected_status else None,
                http_method=http_method if http_method else None,
                verify_ssl=verify_ssl,
            )
            bulk_requests.append(req)

        return bulk_requests

    @staticmethod
    def build_bulk_update_data(
        interval: int | None = None,
        timeout: int | None = None,
        retry_attempts: int | None = None,
    ) -> CheckUpdate:
        """
        Build CheckUpdate schema for bulk modify operation.

        Args:
            interval: Check interval in seconds
            timeout: Timeout in seconds
            retry_attempts: Number of retry attempts

        Returns:
            CheckUpdate schema with specified fields
        """
        update_data = CheckUpdate()
        if interval is not None:
            update_data.interval_seconds = interval
        if timeout is not None:
            update_data.timeout_seconds = timeout
        if retry_attempts is not None:
            update_data.retry_attempts = retry_attempts
        return update_data
