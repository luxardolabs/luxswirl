"""
Check service - business logic for check operations.
"""

import asyncio
import ssl
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from shared.logger import get_logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes

from app.core.check_config import pack_config, take_config_updates
from app.core.check_target_validator import validate_check_target
from app.core.datetime_utils import utc_now
from app.core.exceptions import (
    AgentNotFoundException,
    CheckNotFoundException,
    ValidationException,
)
from app.core.synthetic_guard import (
    SYNTHETIC,
    require_admin_actor,
    validate_script,
)
from app.crud.check_crud import CheckCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.models.check_model import Check
from app.models.enum_model import AssignmentMode, BulkCheckOutcome, CheckType
from app.schemas.agent_schema import AgentCreate
from app.schemas.check_schema import (
    AgentCheckCounts,
    BulkCheckCreateRequest,
    BulkCheckCreateResponse,
    BulkCheckResult,
    CheckCreate,
    CheckInDB,
    CheckUpdate,
    CheckView,
)
from app.schemas.import_export_schema import CheckExport
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.settings_core_service import SettingsCoreService

logger = get_logger("luxswirl.services.check")


class CheckCoreService:
    """Service for check operations."""

    @staticmethod
    async def get_check_by_id(
        db: AsyncSession, check_id: UUID, include_script_code: bool = False
    ) -> Check:
        """Get check by UUID.

        Args:
            db: Database session
            check_id: Check UUID
            include_script_code: If True, load script_code field (deferred TEXT field)

        Returns:
            Check model with agent relationship loaded
        """
        check = await CheckCRUD.get_by_id(
            db, check_id, include_script_code=include_script_code
        )
        if not check:
            raise CheckNotFoundException("unknown", str(check_id))
        return check

    @staticmethod
    async def create_check(
        db: AsyncSession,
        agent_id: UUID,
        data: CheckCreate,
        skip_config_update: bool = False,
        *,
        actor_is_admin: bool = False,
    ) -> Check:
        """Create a new check.

        Args:
            db: Database session
            agent_id: Agent UUID or ID
            data: Check creation data
            skip_config_update: If True, don't update agent.checks_updated_at (for internal/system checks)
        """
        # Get agent
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))

        # SSRF protection: validate check target against network protection settings
        await CheckCoreService._validate_target_ssrf(db, data.target)

        await CheckCoreService._validate_dependency(
            db, getattr(data, "depends_on_check_id", None), own_check_id=None
        )

        check_config = pack_config(data, data.check_type)

        if data.check_type == SYNTHETIC:
            require_admin_actor(actor_is_admin=actor_is_admin)
            if data.script_code:
                validate_script(
                    data.script_code,
                    action="CREATE",
                    check_name=data.display_name,
                    agent_id=agent_id,
                )

        # Create new check (UUID-based, no composite key duplication check needed)
        check = Check(
            agent_id=agent.id,
            display_name=data.display_name,
            check_type=data.check_type,
            target=data.target,
            description=data.description,
            interval_seconds=data.interval_seconds,
            timeout_seconds=data.timeout_seconds,
            enabled=data.enabled,
            retry_attempts=data.retry_attempts,
            retry_interval_seconds=data.retry_interval_seconds,
            resend_notification_after=data.resend_notification_after,
            tags=data.tags,
            check_config=check_config if check_config else None,
            assignment_mode=(
                data.assignment_mode
                if hasattr(data, "assignment_mode")
                else AssignmentMode.MANUAL
            ),
            agent_selector=(
                data.agent_selector if hasattr(data, "agent_selector") else None
            ),
            script_code=data.script_code if hasattr(data, "script_code") else None,
            depends_on_check_id=getattr(data, "depends_on_check_id", None),
        )

        check = await CheckCRUD.add_check(db, check)

        # Apply global alerts as defaults for this new check

        await AlertCoreService.assign_global_alerts_to_check(db, check.id)

        # Update agent's checks_updated_at timestamp to trigger config reload
        # (skip for internal/system checks that are auto-created)
        if not skip_config_update:
            agent.checks_updated_at = utc_now()
            attributes.flag_modified(agent, "checks_updated_at")

        logger.info(
            "Created check",
            extra={"check_name": check.fully_qualified_name, "check_id": str(check.id)},
        )
        return check

    @staticmethod
    async def count_dependents_bulk(
        db: AsyncSession, parent_check_ids: list[UUID]
    ) -> dict[UUID, int]:
        return await CheckCRUD.count_dependents_bulk(db, parent_check_ids)

    @staticmethod
    async def set_dependents(
        db: AsyncSession,
        parent_check_id: UUID,
        child_check_ids: list[UUID],
    ) -> tuple[int, int]:
        """Replace the set of children that depend on a parent.

        Returns (added, removed). Children that already have a different parent
        are reparented to this one. Children that are themselves parents are
        rejected to preserve the single-level rule.
        """
        parent = await CheckCRUD.get_by_id(db, parent_check_id)
        if parent is None:
            raise ValidationException(f"Parent check {parent_check_id} not found")
        if parent.depends_on_check_id is not None:
            raise ValidationException(
                "Selected parent already has its own parent (single-level only)"
            )

        desired = set(child_check_ids)
        if parent_check_id in desired:
            raise ValidationException("A check cannot depend on itself")

        children = await CheckCRUD.get_checks_by_ids(db, list(desired))
        by_id = {c.id: c for c in children}
        missing = desired - by_id.keys()
        if missing:
            raise ValidationException(f"Check {next(iter(missing))} not found")
        already_parents = await CheckCRUD.parents_among(db, list(desired))
        if already_parents:
            name = by_id[next(iter(already_parents))].display_name
            raise ValidationException(
                f"Check '{name}' is itself a parent — single-level only"
            )

        current_children = await CheckCRUD.list_dependents(db, parent_check_id)
        by_id.update({c.id: c for c in current_children})
        current = {c.id for c in current_children}
        to_add = desired - current
        to_remove = current - desired

        await CheckCRUD.set_parent(
            db,
            parent_check_id,
            attach=[by_id[cid] for cid in to_add],
            detach=[by_id[cid] for cid in to_remove],
        )
        logger.info(
            "Bulk updated check dependents",
            extra={
                "parent_check_id": str(parent_check_id),
                "added": len(to_add),
                "removed": len(to_remove),
            },
        )
        return len(to_add), len(to_remove)

    @staticmethod
    async def list_dependents(db: AsyncSession, parent_check_id: UUID) -> list[Check]:
        """Checks that depend on the given parent.

        The predicate is a plain column equality, so the query does it rather
        than loading every check and filtering in Python.
        """
        return list(await CheckCRUD.list_dependents(db, parent_check_id))

    @staticmethod
    async def list_potential_dependents(
        db: AsyncSession, parent_check_id: UUID
    ) -> list[Check]:
        """Checks that *could* become children of this parent.

        Excludes: the parent itself, any check that already has children, and
        the parent's chain ancestor (none in single-level v1, so trivial).
        """
        return await CheckCRUD.list_non_parents(db, parent_check_id)

    @staticmethod
    async def get_dependency_info(db: AsyncSession, check_id: UUID) -> dict[str, Any]:
        """Bundle of parent + parent's latest result + dependent count for UI display."""
        check = await CheckCRUD.get_by_id(db, check_id)
        if check is None:
            return {
                "parent_check": None,
                "parent_latest_result": None,
                "dependent_count": 0,
            }

        parent_check = None
        parent_latest_result = None
        if check.depends_on_check_id is not None:
            parent_check = await CheckCRUD.get_by_id(db, check.depends_on_check_id)
            parent_latest_result = await CheckResultCRUD.get_latest_result_for_check(
                db, check.depends_on_check_id
            )

        dependent_count = await CheckCRUD.count_dependents(db, check_id)

        return {
            "parent_check": parent_check,
            "parent_latest_result": parent_latest_result,
            "dependent_count": dependent_count,
        }

    @staticmethod
    async def list_eligible_parents(
        db: AsyncSession, exclude_check_id: UUID | None = None
    ) -> list[Check]:
        """Checks that may be selected as a parent (excludes those already with a parent)."""
        return await CheckCRUD.list_without_parent(db, exclude_check_id)

    @staticmethod
    async def _validate_dependency(
        db: AsyncSession,
        depends_on_check_id: UUID | None,
        own_check_id: UUID | None,
    ) -> None:
        if depends_on_check_id is None:
            return

        if own_check_id is not None and depends_on_check_id == own_check_id:
            raise ValidationException("A check cannot depend on itself")

        parent = await CheckCRUD.get_by_id(db, depends_on_check_id)
        if parent is None:
            raise ValidationException(f"Parent check {depends_on_check_id} not found")

        if parent.depends_on_check_id is not None:
            raise ValidationException(
                "Single-level dependencies only — the selected parent already has a parent"
            )

    @staticmethod
    async def _validate_target_ssrf(db: AsyncSession, target: str) -> None:
        """Validate a check target against SSRF network protection settings.

        Reads security.block_cloud_metadata and security.block_private_networks
        settings and validates the target accordingly.

        Args:
            db: Database session
            target: The check target URL or host

        Raises:
            CheckTargetBlockedError: If the target is blocked by SSRF protection
        """
        block_cloud_metadata = await SettingsCoreService.get_setting(
            db, "security.block_cloud_metadata", True
        )
        block_private_networks = await SettingsCoreService.get_setting(
            db, "security.block_private_networks", False
        )

        validate_check_target(
            target,
            block_cloud_metadata=block_cloud_metadata,
            block_private_networks=block_private_networks,
        )

    @staticmethod
    async def clone_check(
        db: AsyncSession,
        check_id: UUID,
        agent_id: UUID,
        overrides: CheckCreate | CheckUpdate | None = None,
        skip_config_update: bool = False,
        *,
        actor_is_admin: bool = False,
    ) -> Check:
        """
        Clone an existing check to create a new one.

        This method:
        1. Retrieves the source check configuration
        2. Validates the target agent exists
        3. Merges source config with any user overrides
        4. Creates a new check via create_check()
        5. Copies alert assignments from source
        6. Returns the newly created cloned check

        Args:
            db: Database session
            check_id: UUID of check to clone
            agent_id: UUID of agent to assign cloned check to
            overrides: Optional CheckCreate with fields to override
                       (e.g., new display_name, different interval)
            skip_config_update: If True, skip updating agent.checks_updated_at

        Returns:
            Newly created Check object

        Raises:
            CheckNotFoundException: If source check not found
            AgentNotFoundException: If target agent not found

        Example:
            # Clone with same name
            cloned = await CheckCoreService.clone_check(
                db,
                check_id=source_uuid,
                agent_id=target_agent_uuid
            )

            # Clone with new name
            overrides = CheckCreate(
                display_name="api_health-clone",
                enabled=False  # Start disabled
            )
            cloned = await CheckCoreService.clone_check(
                db,
                check_id=source_uuid,
                agent_id=target_agent_uuid,
                overrides=overrides
            )
        """
        # 1. Fetch source check (include script_code for synthetic checks)
        source = await CheckCoreService.get_check_by_id(
            db, check_id, include_script_code=True
        )
        if not source:
            raise CheckNotFoundException("unknown", str(check_id))

        # 2. Verify target agent exists
        target_agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not target_agent:
            raise AgentNotFoundException(str(agent_id))

        # 3. Build clone data - merge source with overrides
        # Only the fields the caller actually SET override the source; a schema
        # default the caller never sent (timeout_seconds=10, enabled=True, ...)
        # must not silently replace the source's value. None also keeps the source.
        override_fields = (
            overrides.model_dump(exclude_unset=True, exclude_none=True)
            if overrides
            else {}
        )

        # Debug logging for script_code
        source_script_len = len(source.script_code) if source.script_code else 0
        override_script = getattr(overrides, "script_code", None) if overrides else None
        override_script_len = len(override_script) if override_script else 0
        logger.info(
            "Clone script_code sizes",
            extra={
                "source_script_len": source_script_len,
                "override_script_len": override_script_len,
            },
        )

        # The column is nullable, CheckCreate's field is not: a check stored without
        # a timeout clones with the configured default rather than failing validation.
        timeout_seconds: int | None = override_fields.get(
            "timeout_seconds", source.timeout_seconds
        )
        if timeout_seconds is None:
            defaults = await SettingsCoreService.get_check_defaults(db)
            timeout_seconds = defaults.timeout_seconds

        clone_data = CheckCreate(
            display_name=override_fields.get("display_name", source.display_name),
            check_type=override_fields.get("check_type", source.check_type),
            target=override_fields.get("target", source.target),
            description=override_fields.get("description", source.description),
            interval_seconds=override_fields.get(
                "interval_seconds", source.interval_seconds
            ),
            timeout_seconds=timeout_seconds,
            expected_status=override_fields.get(
                "expected_status", source.expected_status
            ),
            enabled=override_fields.get("enabled", source.enabled),
            # HTTP/JSON check fields
            http_method=override_fields.get("http_method", source.http_method),
            verify_ssl=override_fields.get("verify_ssl", source.verify_ssl),
            json_path=override_fields.get("json_path", source.json_path),
            expected_value=override_fields.get("expected_value", source.expected_value),
            # DNS check fields
            record_type=override_fields.get("record_type", source.record_type),
            nameserver=override_fields.get("nameserver", source.nameserver),
            port=override_fields.get("port", source.port),
            expect_value=override_fields.get("expect_value", source.expect_value),
            # MySQL/Postgres check fields
            connection_string=override_fields.get(
                "connection_string", source.connection_string
            ),
            query=override_fields.get("query", source.query),
            # Common fields
            retry_attempts=override_fields.get("retry_attempts", source.retry_attempts),
            retry_interval_seconds=override_fields.get(
                "retry_interval_seconds", source.retry_interval_seconds
            ),
            resend_notification_after=override_fields.get(
                "resend_notification_after", source.resend_notification_after
            ),
            tags=override_fields.get("tags", source.tags),
            assignment_mode=override_fields.get(
                "assignment_mode", source.assignment_mode
            ),
            agent_selector=override_fields.get("agent_selector", source.agent_selector),
            script_code=override_fields.get("script_code", source.script_code),
        )

        # 4. Create new check using existing create_check method
        cloned_check = await CheckCoreService.create_check(
            db,
            agent_id,
            clone_data,
            skip_config_update=skip_config_update,
            actor_is_admin=actor_is_admin,
        )

        # 5. Copy alert assignments from source check

        # A clone that silently loses its alert wiring is a monitoring gap the
        # operator cannot see: the check exists, it runs, and nothing is ever
        # notified about it. Both swallows are gone — a failure rolls the clone
        # back rather than producing a half-wired one. The target is a check
        # created moments ago, so there is no duplicate mapping to hit.
        source_alert_ids = await AlertCoreService.get_alert_ids_for_check(db, check_id)
        for alert_id in source_alert_ids:
            await AlertCoreService.add_check(db, alert_id, cloned_check.id)

        logger.info(
            "Cloned check",
            extra={
                "source_check_name": source.fully_qualified_name,
                "check_id": str(source.id),
                "target_agent_name": target_agent.agent_name,
                "agent_id": str(target_agent.id),
                "cloned_check_name": cloned_check.display_name,
                "cloned_check_id": str(cloned_check.id),
            },
        )

        return cloned_check

    @staticmethod
    async def update_check(
        db: AsyncSession,
        check_id: UUID,
        data: CheckUpdate,
        *,
        actor_is_admin: bool = False,
    ) -> Check:
        """Update a check."""
        check = await CheckCoreService.get_check_by_id(db, check_id)

        update_data = data.model_dump(exclude_unset=True)

        # SSRF protection: validate new target if it's being changed
        if "target" in update_data:
            await CheckCoreService._validate_target_ssrf(db, update_data["target"])

        if "depends_on_check_id" in update_data:
            await CheckCoreService._validate_dependency(
                db, update_data["depends_on_check_id"], own_check_id=check.id
            )

        # The check type after this update, which decides which config fields apply.
        check_type = update_data.get("check_type", check.check_type)

        # Authz (OWASP API5): turning a check INTO a synthetic one, or changing an
        # existing synthetic check's script, both put arbitrary code on an agent host.
        changing_to_synthetic = update_data.get("check_type") == SYNTHETIC
        changing_synthetic_script = check.check_type == SYNTHETIC and bool(
            update_data.get("script_code")
        )
        if changing_to_synthetic or changing_synthetic_script:
            require_admin_actor(actor_is_admin=actor_is_admin)

        # Pops every config name out of update_data, so what is left is columns.
        config_updates = take_config_updates(update_data, check_type)

        logger.info(
            "Config updates",
            extra={
                "check_name": check.display_name,
                "check_id": str(check.id),
                "config_updates": config_updates,
            },
        )

        if config_updates:
            # Merge into existing check_config
            current_config = check.check_config or {}
            current_config.update(config_updates)
            check.check_config = current_config
            # Force SQLAlchemy to detect the JSONB change
            attributes.flag_modified(check, "check_config")
            logger.info(
                "Final check_config",
                extra={
                    "check_name": check.display_name,
                    "check_id": str(check.id),
                    "check_config": check.check_config,
                },
            )

        if check_type == SYNTHETIC and update_data.get("script_code"):
            validate_script(
                update_data["script_code"],
                action="UPDATE",
                check_id=check_id,
                check_name=check.display_name,
            )

        # Update direct model fields
        for field, value in update_data.items():
            setattr(check, field, value)

        check.updated_at = utc_now()

        # Update agent's checks_updated_at timestamp to trigger config reload
        check.agent.checks_updated_at = utc_now()
        attributes.flag_modified(check.agent, "checks_updated_at")

        check = await CheckCRUD.save_check(db, check)

        logger.info(
            "Updated check",
            extra={"check_name": check.fully_qualified_name, "check_id": str(check.id)},
        )
        return check

    @staticmethod
    async def list_checks(
        db: AsyncSession,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        enabled_only: bool = False,
        tag: str | None = None,
        search: str | None = None,
        exclude_internal: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Check], int]:
        """
        List checks with pagination and filtering.

        Args:
            db: Database session
            agent_id: Filter by agent UUID
            check_type: Filter by check type (ping, http, tcp, etc.)
            enabled_only: Only return enabled checks
            tag: Filter by tag (must have this tag)
            search: Search in display_name or target (case-insensitive)
            exclude_internal: Exclude internal/system checks (agent self-monitoring)
            limit: Max results
            offset: Pagination offset

        Returns:
            Tuple of (checks list, total count)
        """
        return await CheckCRUD.list_paginated(
            db,
            agent_id=agent_id,
            check_type=check_type,
            enabled_only=enabled_only,
            tag=tag,
            search=search,
            exclude_internal=exclude_internal,
            skip=offset,
            limit=limit,
        )

    @staticmethod
    async def list_all_checks(db: AsyncSession) -> Sequence[Check]:
        """
        Get all enabled checks (for alert assignment).

        Returns:
            List of all enabled checks
        """
        checks, _ = await CheckCoreService.list_checks(
            db, enabled_only=True, limit=10000
        )
        return checks

    @staticmethod
    async def list_check_rows(
        db: AsyncSession,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        enabled_only: bool = False,
        tag: str | None = None,
        search: str | None = None,
        exclude_internal: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CheckInDB], int]:
        """The paginated check list as the web seam holds it — schemas, not rows.

        The signature mirrors list_checks rather than forwarding **kwargs: an
        untyped passthrough would need a type: ignore, which is the checker
        being silenced at the boundary it exists to hold.
        """
        checks, total = await CheckCoreService.list_checks(
            db,
            agent_id=agent_id,
            check_type=check_type,
            enabled_only=enabled_only,
            tag=tag,
            search=search,
            exclude_internal=exclude_internal,
            limit=limit,
            offset=offset,
        )
        return [CheckInDB.model_validate(c) for c in checks], total

    @staticmethod
    async def list_eligible_parent_rows(
        db: AsyncSession, exclude_check_id: UUID | None = None
    ) -> dict[str, list[CheckInDB]]:
        """Eligible parent checks, grouped by agent name.

        Grouped HERE because the agent name comes from `check.agent`, a
        relationship: the rows are eager-loaded inside this session, and a
        schema that carried the relationship would lazy-load it outside one.
        """
        grouped: dict[str, list[CheckInDB]] = {}
        for c in await CheckCoreService.list_eligible_parents(
            db, exclude_check_id=exclude_check_id
        ):
            agent_name = (
                c.agent.agent_name if c.agent and c.agent.agent_name else "unknown"
            )
            grouped.setdefault(agent_name, []).append(CheckInDB.model_validate(c))
        return grouped

    @staticmethod
    async def check_counts_by_agent(
        db: AsyncSession, agent_ids: Sequence[UUID]
    ) -> dict[UUID, AgentCheckCounts]:
        """Each listed agent's check counts, zero for an agent with none."""
        counts = await CheckCRUD.count_by_agent(db, agent_ids)
        return {
            agent_id: AgentCheckCounts(
                total=counts.get(agent_id, (0, 0))[0],
                enabled=counts.get(agent_id, (0, 0))[1],
            )
            for agent_id in agent_ids
        }

    @staticmethod
    async def list_agent_checks(db: AsyncSession, agent_id: UUID) -> list[CheckInDB]:
        """An agent's checks as the web seam holds them — schemas, not ORM rows.

        list_checks_for_agent stays ORM: the job-to-check, check-result and
        bulk-create paths below read it and go on to mutate what they get.
        """
        return [
            CheckInDB.model_validate(c)
            for c in await CheckCoreService.list_checks_for_agent(db, agent_id)
        ]

    @staticmethod
    async def list_checks_for_agent(
        db: AsyncSession, agent_id: UUID
    ) -> Sequence[Check]:
        """List all checks for an agent."""
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))
        return await CheckCRUD.list_for_agent(db, agent.id)

    @staticmethod
    async def bulk_import_checks(
        db: AsyncSession,
        agent_id: UUID,
        checks: Sequence[Mapping[str, Any]],
        overwrite: bool,
        *,
        actor_is_admin: bool,
    ) -> dict[str, Any]:
        """Import checks for an agent: the ONE import, for the JSON API and the
        web upload's worker job alike.

        Each entry is validated on its own: one that is not a valid check (an
        unknown type or method from an older export) is an error for that check,
        never a refusal of the whole import. `overwrite` replaces an existing
        check of the same name; otherwise it is skipped. `actor_is_admin` is the caller's authority, as for a single
        create: a synthetic check is refused unless it is True.

        Builds the AgentCreate / CheckCreate / CheckUpdate DTOs internally —
        moved here from the api router so routers don't marshal schemas
        (LUXSWIRL-168). Returns {created, updated, skipped, errors}.
        """
        created = updated = skipped = 0
        errors: list[dict[str, str]] = []

        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            await AgentCoreService.create_agent(
                db,
                AgentCreate(
                    agent_name=str(agent_id), hostname=str(agent_id), version="imported"
                ),
            )
            logger.info("Created agent", extra={"agent_id": str(agent_id)})

        existing = await CheckCoreService.list_checks_for_agent(db, agent_id)
        existing_map = {c.display_name: c for c in existing}

        for raw in checks:
            name = str(raw.get("name") or "unknown")
            try:
                check_data = CheckExport.model_validate(raw)
                if check_data.name in existing_map:
                    if not overwrite:
                        skipped += 1
                        continue
                    await CheckCoreService.update_check(
                        db,
                        existing_map[check_data.name].id,
                        CheckUpdate(
                            check_type=check_data.check_type,
                            target=check_data.target,
                            interval_seconds=check_data.interval,
                            timeout_seconds=check_data.timeout,
                            retry_attempts=check_data.retry_attempts,
                            enabled=check_data.enabled,
                            description=check_data.description,
                            http_method=check_data.http_method,
                            expected_status=check_data.expected_status,
                            json_path=check_data.json_path,
                            expected_value=check_data.expected_value,
                            tags=check_data.tags,
                        ),
                        actor_is_admin=actor_is_admin,
                    )
                    updated += 1
                else:
                    await CheckCoreService.create_check(
                        db,
                        agent_id,
                        CheckCreate(
                            display_name=check_data.name,
                            check_type=check_data.check_type,
                            target=check_data.target,
                            interval_seconds=check_data.interval,
                            timeout_seconds=check_data.timeout,
                            retry_attempts=check_data.retry_attempts,
                            enabled=check_data.enabled,
                            description=check_data.description,
                            http_method=check_data.http_method,
                            expected_status=check_data.expected_status,
                            json_path=check_data.json_path,
                            expected_value=check_data.expected_value,
                            tags=check_data.tags,
                        ),
                        actor_is_admin=actor_is_admin,
                    )
                    created += 1
            except Exception as e:
                errors.append({"check": name, "error": str(e)})
                logger.exception("Error importing check", extra={"check_name": name})

        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
        }

    @staticmethod
    async def delete_check(db: AsyncSession, check_id: UUID) -> None:
        """Delete a check."""
        check = await CheckCoreService.get_check_by_id(db, check_id)

        # Get agent before deleting check
        await CheckCRUD.load_agent(db, check)
        agent = check.agent

        await CheckCRUD.delete_check(db, check)

        # Update agent's checks_updated_at timestamp to trigger config reload
        agent.checks_updated_at = utc_now()
        attributes.flag_modified(agent, "checks_updated_at")

        logger.info(
            "Deleted check",
            extra={"check_name": check.fully_qualified_name, "check_id": str(check.id)},
        )

    @staticmethod
    async def _touch_agents_of(db: AsyncSession, check_ids: list[UUID]) -> int:
        """Bump checks_updated_at on every agent owning one of `check_ids`, so
        each reloads its config on its next heartbeat. Returns how many."""
        checks = await CheckCRUD.get_with_agent_by_ids(db, check_ids)
        affected_agents = {check.agent for check in checks}
        for agent in affected_agents:
            agent.checks_updated_at = utc_now()
            attributes.flag_modified(agent, "checks_updated_at")
        return len(affected_agents)

    @staticmethod
    async def bulk_set_enabled(
        db: AsyncSession, check_ids: list[UUID], *, enabled: bool
    ) -> dict[str, Any]:
        """Enable or disable `check_ids`: an in-place UPDATE, safe in a request.

        Returns:
            {"success_count", "failure_count", "errors"}
        """
        if not check_ids:
            return {"success_count": 0, "failure_count": 0, "errors": []}
        # swallowed-exceptions: a DB failure is reported as failures; the savepoint keeps the caller's session committable, which is what delivers that report
        try:
            async with db.begin_nested():
                success_count = await CheckCRUD.bulk_set_enabled(db, check_ids, enabled)
                await CheckCoreService._touch_agents_of(db, check_ids)
        except SQLAlchemyError as e:
            logger.exception("Failed to bulk-toggle checks", extra={"enabled": enabled})
            return {
                "success_count": 0,
                "failure_count": len(check_ids),
                "errors": [str(e)],
            }
        logger.info(
            "Bulk toggle complete",
            extra={"enabled": enabled, "success_count": success_count},
        )
        return CheckCoreService._bulk_outcome(check_ids, success_count)

    @staticmethod
    async def bulk_delete(db: AsyncSession, check_ids: list[UUID]) -> dict[str, Any]:
        """Delete `check_ids`, cascading through their results.

        The cascade is long, so this runs only in the maintenance worker (the
        bulk_check_delete job), never inside a web request.

        Returns:
            {"success_count", "failure_count", "errors"}
        """
        if not check_ids:
            return {"success_count": 0, "failure_count": 0, "errors": []}
        # swallowed-exceptions: a DB failure is reported as failures; the savepoint keeps the worker's session committable, which is what delivers that report
        try:
            async with db.begin_nested():
                affected = await CheckCoreService._touch_agents_of(db, check_ids)
                success_count = await CheckCRUD.bulk_delete_by_ids(db, check_ids)
        except SQLAlchemyError as e:
            logger.exception("Failed to bulk-delete checks")
            return {
                "success_count": 0,
                "failure_count": len(check_ids),
                "errors": [str(e)],
            }
        logger.info(
            "Bulk deleted checks",
            extra={"success_count": success_count, "affected_agent_count": affected},
        )
        return CheckCoreService._bulk_outcome(check_ids, success_count)

    @staticmethod
    def _bulk_outcome(check_ids: list[UUID], success_count: int) -> dict[str, Any]:
        """A bulk result whose counts add up to the checks asked for: an id the
        statement did not touch (no such check) is a failure, not a silence."""
        missing = len(set(check_ids)) - success_count
        return {
            "success_count": success_count,
            "failure_count": missing,
            "errors": [f"{missing} check(s) not found"] if missing else [],
        }

    @staticmethod
    async def bulk_modify(
        db: AsyncSession,
        check_ids: list[UUID],
        update_data: CheckUpdate,
        new_agent_id: UUID | None = None,
    ) -> dict[str, Any]:
        """
        Bulk modify multiple checks.

        Args:
            db: Database session
            check_ids: List of check UUIDs to modify
            update_data: CheckUpdate with fields to modify
            new_agent_name: Optional new agent name (will update agent_id to preserve historical data)

        Returns:
            Dict with success/failure counts
        """

        success_count = 0
        failure_count = 0
        errors = []
        affected_agents = set()  # Track agents that need config_version update

        # One query for the whole selection, and one for the target agent —
        # both were being re-issued per check, and the agent is the same every
        # time round the loop. A check id that no longer resolves still fails
        # on its own below, exactly as the per-check read did.
        checks_by_id = {
            check.id: check
            for check in await CheckCRUD.get_checks_by_ids(db, check_ids)
        }
        # get_agent_by_id RAISES when the agent is gone, so resolve it here and
        # keep the miss as a per-check failure — the loop used to record one
        # failure per check for a bad target, and callers count on that shape.
        new_agent = None
        new_agent_error: str | None = None
        if new_agent_id:
            try:
                new_agent = await AgentCoreService.get_agent_by_id(db, new_agent_id)
            except AgentNotFoundException:
                new_agent_error = f"Target agent {new_agent_id} not found"

        for check_id in check_ids:
            try:
                check = checks_by_id.get(check_id)
                if not check:
                    raise CheckNotFoundException("unknown", str(check_id))

                # If changing agent, update the agent_id to preserve historical data
                if new_agent_id and new_agent_id != check.agent_id:
                    if not new_agent:
                        raise ValueError(new_agent_error or "Target agent not found")

                    # Track both old and new agents for config sync
                    old_agent = check.agent
                    affected_agents.add(old_agent)
                    affected_agents.add(new_agent)

                    # Update agent_id while preserving all historical data
                    check.agent_id = new_agent.id

                    # Also apply any other updates from update_data
                    if update_data.interval_seconds is not None:
                        check.interval_seconds = update_data.interval_seconds
                    if update_data.timeout_seconds is not None:
                        check.timeout_seconds = update_data.timeout_seconds
                    if update_data.retry_attempts is not None:
                        check.retry_attempts = update_data.retry_attempts
                    if update_data.enabled is not None:
                        check.enabled = update_data.enabled
                    if update_data.tags is not None:
                        check.tags = update_data.tags
                    if update_data.description is not None:
                        check.description = update_data.description
                else:
                    # Just update in place (update_check handles checks_updated_at)
                    await CheckCoreService.update_check(db, check.id, update_data)
                    # No need to track agent here as update_check already handles it

                success_count += 1

            except Exception as e:
                logger.warning(
                    "Failed to modify check",
                    extra={"check_id": str(check_id)},
                    exc_info=True,
                )
                errors.append(str(e))
                failure_count += 1

        # Update checks_updated_at for all affected agents to trigger config reload
        if affected_agents:
            for agent in affected_agents:
                agent.checks_updated_at = utc_now()
                attributes.flag_modified(agent, "checks_updated_at")
            logger.info(
                "Bulk modify complete",
                extra={
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "affected_agent_count": len(affected_agents),
                },
            )
        else:
            logger.info(
                "Bulk modify complete",
                extra={"success_count": success_count, "failure_count": failure_count},
            )

        return {
            "success_count": success_count,
            "failure_count": failure_count,
            "errors": errors,
        }

    @staticmethod
    async def bulk_create_checks(
        db: AsyncSession,
        agent_id: UUID,
        requests: list[BulkCheckCreateRequest],
    ) -> BulkCheckCreateResponse:
        """
        Bulk create checks from a list of URLs.

        Args:
            db: Database session
            agent_id: Agent UUID or ID
            requests: List of BulkCheckCreateRequest objects

        Returns:
            Bulk creation results:
            {
                "total": int,
                "succeeded": int,
                "failed": int,
                "results": [BulkCheckResult, ...]
            }
        """

        results: list[BulkCheckResult] = []
        succeeded = 0
        failed = 0

        for req in requests:
            try:
                # Parse URL to detect check type
                url = req.url.strip()
                parsed = urlparse(url)

                # Detect check type from scheme
                if parsed.scheme in ("http", "https"):
                    check_type = (
                        "http"  # Check type is "http" for both http:// and https://
                    )
                elif parsed.scheme == "tcp":
                    check_type = "tcp"
                elif not parsed.scheme:
                    # No scheme provided, assume HTTP/HTTPS
                    check_type = "http"
                    url = f"https://{url}"
                    parsed = urlparse(url)
                else:
                    check_type = parsed.scheme

                # Generate display name if not provided
                if req.display_name:
                    display_name = req.display_name
                else:
                    # Generate from hostname and path
                    hostname = parsed.hostname or "unknown"
                    # Replace dots and slashes with hyphens
                    display_name = hostname.replace(".", "-")
                    if parsed.path and parsed.path != "/":
                        path_part = parsed.path.strip("/").replace("/", "-")
                        display_name = f"{display_name}-{path_part}"
                    if parsed.port and parsed.port not in (80, 443):
                        display_name = f"{display_name}-{parsed.port}"
                    # Truncate if too long
                    if len(display_name) > 255:
                        display_name = display_name[:255]

                # Build CheckCreate object
                check_data = CheckCreate(
                    display_name=display_name,
                    check_type=CheckType(check_type),
                    target=url,
                    interval_seconds=req.interval_seconds,
                    timeout_seconds=req.timeout_seconds or 10,
                    enabled=req.enabled,
                    tags=req.tags,
                    expected_status=req.expected_status,
                    http_method=req.http_method,
                    verify_ssl=req.verify_ssl,
                )

                # Create the check
                check = await CheckCoreService.create_check(db, agent_id, check_data)

                # Record success
                results.append(
                    BulkCheckResult(
                        url=url,
                        status=BulkCheckOutcome.SUCCESS,
                        check_id=check.id,
                        display_name=display_name,
                        check_type=check_data.check_type,
                    )
                )
                succeeded += 1

            except Exception as e:
                # Record failure
                logger.warning(
                    "Failed to create check for URL",
                    extra={"url": req.url},
                    exc_info=True,
                )
                results.append(
                    BulkCheckResult(
                        url=req.url,
                        status=BulkCheckOutcome.FAILED,
                        display_name=req.display_name,
                        error=str(e),
                    )
                )
                failed += 1

        logger.info(
            "Bulk create complete",
            extra={
                "succeeded": succeeded,
                "failed": failed,
                "total": len(requests),
            },
        )
        return BulkCheckCreateResponse(
            total=len(requests),
            succeeded=succeeded,
            failed=failed,
            results=results,
        )

    @staticmethod
    async def bulk_preview_checks(urls: list[str]) -> list[dict[str, Any]]:
        """
        Preview and validate bulk check URLs.

        Args:
            urls: List of URL strings to preview

        Returns:
            List of dicts with url, generated_name, check_type, validation_status, etc.
        """

        async def validate_url(url_str: str) -> dict[str, Any]:
            """Validate a single URL by fetching it."""
            try:
                # Parse URL to detect check type
                parsed = urlparse(url_str)

                # Detect check type from scheme
                if parsed.scheme in ("http", "https"):
                    check_type = (
                        "http"  # Check type is "http" for both http:// and https://
                    )
                    full_url = url_str
                elif not parsed.scheme:
                    # Default to https if no scheme
                    check_type = "http"
                    full_url = f"https://{url_str}"
                    parsed = urlparse(full_url)
                else:
                    check_type = parsed.scheme  # tcp, etc.
                    full_url = url_str

                # Generate display name (same logic as bulk_create_checks)
                hostname = parsed.hostname or "unknown"
                display_name = hostname.replace(".", "-")
                if parsed.path and parsed.path != "/":
                    path_part = parsed.path.strip("/").replace("/", "-")
                    display_name = f"{display_name}-{path_part}"
                if parsed.port and parsed.port not in (80, 443):
                    display_name = f"{display_name}-{parsed.port}"
                # Truncate if too long
                if len(display_name) > 255:
                    display_name = display_name[:255]

                # Validate by fetching (for HTTP/HTTPS only)
                validation_status = "unknown"
                validation_message = ""
                status_code = None

                if check_type == "http":
                    # Certificates ARE verified here, because the agent's http
                    # check defaults to verify_ssl=True. The preview used to pass
                    # verify=False, so a self-signed target previewed as
                    # "success" and then failed on its first real run — the
                    # preview was answering a different question from the check
                    # it previews. A cert problem is reported as its own warning
                    # so the operator can set verify_ssl=False deliberately.
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            response = await client.get(full_url)
                            status_code = response.status_code
                            if 200 <= status_code < 400:
                                validation_status = "success"
                                validation_message = f"HTTP {status_code}"
                            else:
                                validation_status = "warning"
                                validation_message = f"HTTP {status_code}"
                    except httpx.TimeoutException:
                        validation_status = "error"
                        validation_message = "Timeout (5s)"
                    except httpx.ConnectError as e:
                        # httpx wraps a TLS handshake failure in ConnectError, so
                        # this has to be distinguished by the cause, not the type.
                        if isinstance(e.__cause__, ssl.SSLError):
                            validation_status = "warning"
                            validation_message = (
                                "TLS certificate not trusted — set verify_ssl=False "
                                "on the check to monitor it anyway"
                            )
                        else:
                            validation_status = "error"
                            validation_message = "Connection refused"
                    except Exception as e:
                        validation_status = "error"
                        validation_message = str(e)[:100]

                return {
                    "url": full_url,
                    "generated_name": display_name,
                    "check_type": check_type,
                    "validation_status": validation_status,
                    "validation_message": validation_message,
                    "status_code": status_code,
                }
            # swallowed-exceptions: per-URL bulk preview — the failure IS the
            # result, returned as an error row the operator sees before importing.
            except Exception as e:
                return {
                    "url": url_str,
                    "generated_name": url_str.replace(".", "-")[:255],
                    "check_type": "http",
                    "validation_status": "error",
                    "validation_message": f"Parse error: {str(e)[:100]}",
                }

        # Validate all URLs in parallel
        previews = await asyncio.gather(*[validate_url(url) for url in urls])
        return previews

    @staticmethod
    async def assign_alerts(
        db: AsyncSession, check_id: UUID, alert_ids: list[UUID]
    ) -> None:
        """
        Assign multiple alerts to a check.

        Args:
            db: Database session
            check_id: Check UUID
            alert_ids: List of alert IDs to assign

        Raises:
            CheckNotFoundException: If check not found
        """

        # Verify check exists
        await CheckCoreService.get_check_by_id(db, check_id)

        # Add check to each alert. Unlike AlertCoreService.assign_alerts this
        # takes a raw list rather than a set difference, so "already added" is a
        # real possibility and is the state the caller asked for — that one is
        # caught by TYPE and treated as satisfied. Everything else propagates:
        # the broad swallow here returned success while the check was wired to
        # fewer alerts than requested. (LUXSWIRL-215)
        for alert_id in alert_ids:
            try:
                await AlertCoreService.add_check(db, alert_id, check_id)
            except ValidationException:
                logger.debug(
                    "Check already assigned to alert — nothing to do",
                    extra={"check_id": str(check_id), "alert_id": str(alert_id)},
                )

        logger.info(
            "Assigned check to alerts",
            extra={"check_id": str(check_id), "alert_count": len(alert_ids)},
        )

    @staticmethod
    async def create_check_with_alerts(
        db: AsyncSession,
        agent_id: UUID,
        data: CheckCreate,
        alert_ids: list[UUID],
        *,
        actor_is_admin: bool = False,
    ) -> CheckView:
        """Create a check and assign the supplied alerts in one workflow."""
        check = await CheckCoreService.create_check(
            db, agent_id, data, actor_is_admin=actor_is_admin
        )
        await CheckCoreService.assign_alerts(db, check.id, alert_ids)
        return CheckView.model_validate(check)

    @staticmethod
    async def update_check_with_alerts(
        db: AsyncSession,
        check_id: UUID,
        data: CheckUpdate,
        alert_ids: list[UUID] | None = None,
        *,
        actor_is_admin: bool = False,
    ) -> CheckView:
        """Update a check and optionally sync the alert assignment list."""
        check = await CheckCoreService.update_check(
            db, check_id, data, actor_is_admin=actor_is_admin
        )
        if alert_ids is not None:
            await AlertCoreService.sync_check_alerts(db, check.id, alert_ids)
        return CheckView.model_validate(check)

    @staticmethod
    async def clone_check_with_alerts(
        db: AsyncSession,
        source_check_id: UUID,
        target_agent_id: UUID,
        overrides: CheckCreate | None,
        alert_ids: list[UUID],
        *,
        actor_is_admin: bool = False,
    ) -> CheckView:
        """Clone a check and assign the supplied alerts in one workflow."""
        cloned_check = await CheckCoreService.clone_check(
            db,
            source_check_id,
            target_agent_id,
            overrides,
            actor_is_admin=actor_is_admin,
        )
        await AlertCoreService.sync_check_alerts(db, cloned_check.id, alert_ids)
        return CheckView.model_validate(cloned_check)

    @staticmethod
    async def get_distinct_check_types(db: AsyncSession) -> list[str]:
        """
        Get all unique check types from checks.

        Args:
            db: Database session

        Returns:
            The in-use check types that are valid CheckType members.
        """
        valid = {t.value for t in CheckType}
        return [t for t in await CheckCRUD.get_distinct_check_types(db) if t in valid]

    @staticmethod
    async def get_all_check_tags(db: AsyncSession) -> list[str]:
        """
        Get all unique tags from all checks.

        Args:
            db: Database session

        Returns:
            Sorted list of unique tag strings
        """
        return await CheckCRUD.get_all_check_tags(db)

    @staticmethod
    async def get_all_tags_combined(db: AsyncSession) -> list[str]:
        """
        Get all distinct tags from both checks (array) and agents (comma-separated).

        Args:
            db: Database session

        Returns:
            Sorted list of unique tag strings
        """
        return await CheckCRUD.get_all_tags_combined(db)

    @staticmethod
    async def get_checks_by_ids(db: AsyncSession, check_ids: list[UUID]) -> list[Check]:
        """
        Get multiple checks by their IDs with agent relationship loaded.

        Args:
            db: Database session
            check_ids: List of check UUIDs

        Returns:
            List of Check objects with agent loaded
        """
        return await CheckCRUD.get_checks_by_ids(db, check_ids)
