"""
Check service - business logic for check operations.
"""

import asyncio
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes

from app.core.check_target_validator import validate_check_target
from app.core.datetime_utils import utc_now
from app.core.exceptions import (
    AgentNotFoundException,
    AuthorizationException,
    CheckNotFoundException,
    ValidationException,
)
from app.core.synthetic_security import SyntheticSecurityError, validate_and_raise
from app.crud.check_crud import CheckCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.models.check_model import Check
from app.models.enum_model import AssignmentMode, CheckType
from app.schemas.agent_schema import AgentCreate
from app.schemas.check_schema import CheckCreate, CheckListResponse, CheckResponse, CheckUpdate
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
        check = await CheckCRUD.get_by_id(db, check_id, include_script_code=include_script_code)
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

        # Pack check-type-specific fields into check_config JSONB (filter by check type)
        check_config = {}

        # Define which fields are valid for each check type
        type_fields = {
            "http": ["http_method", "verify_ssl", "expected_status"],
            "json": [
                "http_method",
                "verify_ssl",
                "expected_status",
                "json_path",
                "expected_value",
            ],
            "dns": ["record_type", "nameserver", "port", "expect_value"],
            "mysql": ["connection_string", "query"],
            "postgres": ["connection_string", "query"],
        }

        # Get relevant fields for this check type
        relevant_fields = type_fields.get(data.check_type, [])

        for field in relevant_fields:
            value = getattr(data, field, None)
            # Only store non-None, non-empty values
            if value is not None and value != "":
                check_config[field] = value

        # Authoritative function-level authz (OWASP API5). Synthetic checks execute
        # arbitrary Python on the agent host, so only an admin actor may create them.
        # Enforced here — not just in the web view — so the JSON API path cannot bypass
        # it. API Bearer tokens are admin-equivalent and pass actor_is_admin=True (see
        # SECURITY.md); the AST validator below is hardening, NOT the access control.
        if data.check_type == "synthetic" and not actor_is_admin:
            raise AuthorizationException("Synthetic checks require admin privileges")

        # Security: Validate synthetic check scripts using AST analysis
        # This blocks obvious attacks but is not a complete sandbox
        # Synthetic checks should only be used in trusted, self-hosted environments
        if data.check_type == "synthetic" and hasattr(data, "script_code") and data.script_code:
            try:
                validate_and_raise(data.script_code)
                # SECURITY AUDIT: Log successful validation
                logger.warning(
                    "SECURITY AUDIT: Synthetic check script validation passed",
                    extra={
                        "event": "synthetic_check_create",
                        "check_name": data.display_name,
                        "agent_id": str(agent_id),
                        "script_length": len(data.script_code),
                        "action": "CREATE",
                    },
                )
            except SyntheticSecurityError:
                # SECURITY AUDIT: Log failed validation
                logger.error(
                    "SECURITY AUDIT: Synthetic check script validation FAILED",
                    extra={
                        "event": "synthetic_check_blocked",
                        "check_name": data.display_name,
                        "agent_id": str(agent_id),
                        "action": "CREATE",
                    },
                    exc_info=True,
                )
                raise

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
                data.assignment_mode if hasattr(data, "assignment_mode") else AssignmentMode.MANUAL
            ),
            agent_selector=(data.agent_selector if hasattr(data, "agent_selector") else None),
            script_code=data.script_code if hasattr(data, "script_code") else None,
            depends_on_check_id=getattr(data, "depends_on_check_id", None),
        )

        db.add(check)
        await db.flush()
        await db.refresh(check, ["agent"])

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

        all_checks = await CheckCRUD.list_all(db)
        by_id = {c.id: c for c in all_checks}

        for cid in desired:
            child = by_id.get(cid)
            if child is None:
                raise ValidationException(f"Check {cid} not found")
            child_dependents = sum(1 for c in all_checks if c.depends_on_check_id == cid)
            if child_dependents > 0:
                raise ValidationException(
                    f"Check '{child.display_name}' is itself a parent — single-level only"
                )

        current = {c.id for c in all_checks if c.depends_on_check_id == parent_check_id}
        to_add = desired - current
        to_remove = current - desired

        for cid in to_add:
            by_id[cid].depends_on_check_id = parent_check_id
        for cid in to_remove:
            by_id[cid].depends_on_check_id = None

        await db.flush()
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
        all_checks = await CheckCRUD.list_all(db)
        return [c for c in all_checks if c.depends_on_check_id == parent_check_id]

    @staticmethod
    async def list_potential_dependents(db: AsyncSession, parent_check_id: UUID) -> list[Check]:
        """Checks that *could* become children of this parent.

        Excludes: the parent itself, any check that already has children, and
        the parent's chain ancestor (none in single-level v1, so trivial).
        """
        all_checks = await CheckCRUD.list_all(db)
        parents_of_others = {c.depends_on_check_id for c in all_checks if c.depends_on_check_id}
        return [c for c in all_checks if c.id != parent_check_id and c.id not in parents_of_others]

    @staticmethod
    async def get_dependency_info(db: AsyncSession, check_id: UUID) -> dict:
        """Bundle of parent + parent's latest result + dependent count for UI display."""
        check = await CheckCRUD.get_by_id(db, check_id)
        if check is None:
            return {"parent_check": None, "parent_latest_result": None, "dependent_count": 0}

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
        all_checks = await CheckCRUD.list_all(db)
        return [c for c in all_checks if c.depends_on_check_id is None and c.id != exclude_check_id]

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
        source_check_id: UUID,
        target_agent_id: UUID,
        overrides: CheckCreate | None = None,
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
            source_check_id: UUID of check to clone
            target_agent_id: UUID of agent to assign cloned check to
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
                source_check_id=source_uuid,
                target_agent_id=target_agent_uuid
            )

            # Clone with new name
            overrides = CheckCreate(
                display_name="api_health-clone",
                enabled=False  # Start disabled
            )
            cloned = await CheckCoreService.clone_check(
                db,
                source_check_id=source_uuid,
                target_agent_id=target_agent_uuid,
                overrides=overrides
            )
        """
        # 1. Fetch source check (include script_code for synthetic checks)
        source = await CheckCoreService.get_check_by_id(
            db, source_check_id, include_script_code=True
        )
        if not source:
            raise CheckNotFoundException("unknown", str(source_check_id))

        # 2. Verify target agent exists
        target_agent = await AgentCoreService.get_agent_by_id(db, target_agent_id)
        if not target_agent:
            raise AgentNotFoundException(str(target_agent_id))

        # 3. Build clone data - merge source with overrides
        def get_override(field_name: str, source_value):
            """Get override value or use source value."""
            if overrides and hasattr(overrides, field_name):
                override_value = getattr(overrides, field_name)
                # Handle None vs unset distinction
                if override_value is not None:
                    return override_value
            return source_value

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

        clone_data = CheckCreate(
            display_name=get_override("display_name", source.display_name),
            check_type=get_override("check_type", source.check_type),
            target=get_override("target", source.target),
            description=get_override("description", source.description),
            interval_seconds=get_override("interval_seconds", source.interval_seconds),
            timeout_seconds=get_override("timeout_seconds", source.timeout_seconds),
            expected_status=get_override("expected_status", source.expected_status),
            enabled=get_override("enabled", source.enabled),
            # HTTP/JSON check fields
            http_method=get_override("http_method", source.http_method),
            verify_ssl=get_override("verify_ssl", source.verify_ssl),
            json_path=get_override("json_path", source.json_path),
            expected_value=get_override("expected_value", source.expected_value),
            # DNS check fields
            record_type=get_override("record_type", source.record_type),
            nameserver=get_override("nameserver", source.nameserver),
            port=get_override("port", source.port),
            expect_value=get_override("expect_value", source.expect_value),
            # MySQL/Postgres check fields
            connection_string=get_override("connection_string", source.connection_string),
            query=get_override("query", source.query),
            # Common fields
            retry_attempts=get_override("retry_attempts", source.retry_attempts),
            retry_interval_seconds=get_override(
                "retry_interval_seconds", source.retry_interval_seconds
            ),
            resend_notification_after=get_override(
                "resend_notification_after", source.resend_notification_after
            ),
            tags=get_override("tags", source.tags),
            assignment_mode=get_override("assignment_mode", source.assignment_mode),
            agent_selector=get_override("agent_selector", source.agent_selector),
            script_code=get_override("script_code", source.script_code),
        )

        # 4. Create new check using existing create_check method
        cloned_check = await CheckCoreService.create_check(
            db,
            target_agent_id,
            clone_data,
            skip_config_update=skip_config_update,
            actor_is_admin=actor_is_admin,
        )

        # 5. Copy alert assignments from source check

        try:
            source_alert_ids = await AlertCoreService.get_alert_ids_for_check(db, source_check_id)
            for alert_id in source_alert_ids:
                try:
                    await AlertCoreService.add_check(db, alert_id, cloned_check.id)
                except Exception:
                    logger.warning(
                        "Failed to copy alert to cloned check",
                        extra={"alert_id": str(alert_id), "cloned_check_id": str(cloned_check.id)},
                        exc_info=True,
                    )
        except Exception:
            logger.warning(
                "Failed to copy alerts from source check",
                extra={"source_check_id": str(source_check_id)},
                exc_info=True,
            )

        logger.info(
            "Cloned check",
            extra={
                "source_check_name": source.fully_qualified_name,
                "source_check_id": str(source.id),
                "target_agent_name": target_agent.agent_name,
                "target_agent_id": str(target_agent.id),
                "cloned_check_name": cloned_check.display_name,
                "cloned_check_id": str(cloned_check.id),
            },
        )

        return cloned_check

    @staticmethod
    async def update_check(
        db: AsyncSession, check_id: UUID, data: CheckUpdate, *, actor_is_admin: bool = False
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

        # Define which fields are valid for each check type
        type_fields = {
            "http": ["http_method", "verify_ssl", "expected_status"],
            "json": [
                "http_method",
                "verify_ssl",
                "expected_status",
                "json_path",
                "expected_value",
            ],
            "dns": ["record_type", "nameserver", "port", "expect_value"],
            "mysql": ["connection_string", "query"],
            "postgres": ["connection_string", "query"],
        }

        # All possible config fields (must be removed from update_data)
        all_config_fields = [
            "http_method",
            "verify_ssl",
            "expected_status",
            "json_path",
            "expected_value",
            "record_type",
            "nameserver",
            "port",
            "expect_value",
            "connection_string",
            "query",
        ]

        # Get the check type (from update data or existing check)
        check_type = update_data.get("check_type", check.check_type)
        relevant_fields = type_fields.get(check_type, [])

        # Authz (OWASP API5): only an admin actor may turn a check into a synthetic one
        # or change a synthetic check's script (arbitrary code on the agent host).
        # Enforced in core so the JSON API path cannot bypass the web-layer gate.
        changing_to_synthetic = update_data.get("check_type") == "synthetic"
        changing_synthetic_script = check.check_type == "synthetic" and bool(
            update_data.get("script_code")
        )
        if (changing_to_synthetic or changing_synthetic_script) and not actor_is_admin:
            raise AuthorizationException("Synthetic checks require admin privileges")

        # Update check_config fields (only relevant fields for this check type)
        config_updates = {}
        for field in relevant_fields:
            if field in update_data:
                value = update_data.pop(field)
                # Only store non-None, non-empty values
                if value is not None and value != "":
                    config_updates[field] = value

        # Remove ALL remaining config fields from update_data (they're not direct model fields)
        for field in all_config_fields:
            update_data.pop(field, None)

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

        # Security: Validate synthetic check scripts if script_code is being updated
        if (
            check_type == "synthetic"
            and "script_code" in update_data
            and update_data.get("script_code")
        ):
            try:
                validate_and_raise(update_data["script_code"])
                # SECURITY AUDIT: Log successful validation
                logger.warning(
                    "SECURITY AUDIT: Synthetic check script validation passed",
                    extra={
                        "event": "synthetic_check_update",
                        "check_id": str(check_id),
                        "check_name": check.display_name,
                        "script_length": len(update_data["script_code"]),
                        "action": "UPDATE",
                    },
                )
            except SyntheticSecurityError:
                # SECURITY AUDIT: Log failed validation
                logger.error(
                    "SECURITY AUDIT: Synthetic check script validation FAILED",
                    exc_info=True,
                    extra={
                        "event": "synthetic_check_blocked",
                        "check_id": str(check_id),
                        "check_name": check.display_name,
                        "action": "UPDATE",
                    },
                )
                raise

        # Update direct model fields
        for field, value in update_data.items():
            setattr(check, field, value)

        check.updated_at = utc_now()

        # Update agent's checks_updated_at timestamp to trigger config reload
        check.agent.checks_updated_at = utc_now()
        attributes.flag_modified(check.agent, "checks_updated_at")

        await db.flush()
        await db.refresh(check, ["agent"])

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
            offset=offset,
            limit=limit,
        )

    @staticmethod
    async def list_all_checks(db: AsyncSession) -> Sequence[Check]:
        """
        Get all enabled checks (for alert assignment).

        Returns:
            List of all enabled checks
        """
        checks, _ = await CheckCoreService.list_checks(db, enabled_only=True, limit=10000)
        return checks

    @staticmethod
    async def list_checks_for_agent(db: AsyncSession, agent_id: UUID) -> Sequence[Check]:
        """List all checks for an agent."""
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))
        return await CheckCRUD.list_for_agent(db, agent.id)

    @staticmethod
    async def bulk_import_checks(
        db: AsyncSession, agent_id: UUID, checks: list[CheckExport], overwrite: bool
    ) -> dict[str, Any]:
        """Import checks for an agent (creating the agent if missing).

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
                AgentCreate(agent_name=str(agent_id), hostname=str(agent_id), version="imported"),
            )
            logger.info("Created agent", extra={"agent_id": str(agent_id)})

        existing = await CheckCoreService.list_checks_for_agent(db, agent_id)
        existing_map = {c.display_name: c for c in existing}

        for check_data in checks:
            try:
                if check_data.name in existing_map:
                    if not overwrite:
                        skipped += 1
                        continue
                    await CheckCoreService.update_check(
                        db,
                        existing_map[check_data.name].id,
                        CheckUpdate(
                            check_type=CheckType(check_data.check_type),
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
                    )
                    updated += 1
                else:
                    await CheckCoreService.create_check(
                        db,
                        agent_id,
                        CheckCreate(
                            display_name=check_data.name,
                            check_type=CheckType(check_data.check_type),
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
                    )
                    created += 1
            except Exception as e:  # noqa: BLE001 — per-check isolation, collect + continue
                errors.append({"check": check_data.name, "error": str(e)})
                logger.error(
                    "Error importing check",
                    extra={"check_name": check_data.name},
                    exc_info=True,
                )

        return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}

    @staticmethod
    async def delete_check(db: AsyncSession, check_id: UUID) -> None:
        """Delete a check."""
        check = await CheckCoreService.get_check_by_id(db, check_id)

        # Get agent before deleting check
        await db.refresh(check, ["agent"])
        agent = check.agent

        await db.delete(check)
        await db.flush()

        # Update agent's checks_updated_at timestamp to trigger config reload
        agent.checks_updated_at = utc_now()
        attributes.flag_modified(agent, "checks_updated_at")

        logger.info(
            "Deleted check",
            extra={"check_name": check.fully_qualified_name, "check_id": str(check.id)},
        )

    @staticmethod
    async def bulk_action(
        db: AsyncSession,
        check_ids: list[UUID],
        action: str,
    ) -> dict:
        """
        Perform bulk action on multiple checks.

        Args:
            db: Database session
            check_ids: List of check UUIDs to process
            action: Action to perform ("delete", "disable", "enable")

        Returns:
            Dict with success/failure counts
        """

        if not check_ids:
            return {"success_count": 0, "failure_count": 0, "errors": []}

        success_count = 0
        failure_count = 0
        errors = []

        try:
            if action == "delete":
                checks = await CheckCRUD.get_with_agent_by_ids(db, check_ids)
                affected_agents = {check.agent for check in checks}

                success_count = await CheckCRUD.bulk_delete_by_ids(db, check_ids)

                for agent in affected_agents:
                    agent.checks_updated_at = utc_now()
                    attributes.flag_modified(agent, "checks_updated_at")

                logger.info(
                    "Bulk deleted checks",
                    extra={
                        "success_count": success_count,
                        "affected_agent_count": len(affected_agents),
                    },
                )

            elif action in ("disable", "enable"):
                enabled = action == "enable"
                success_count = await CheckCRUD.bulk_set_enabled(db, check_ids, enabled)

                checks = await CheckCRUD.get_with_agent_by_ids(db, check_ids)
                affected_agents = {check.agent for check in checks}
                for agent in affected_agents:
                    agent.checks_updated_at = utc_now()
                    attributes.flag_modified(agent, "checks_updated_at")

                logger.info(
                    "Bulk action complete",
                    extra={"action": action, "success_count": success_count},
                )

            else:
                logger.warning(
                    "Bulk action: unknown action",
                    extra={"action": action, "check_count": len(check_ids)},
                )
                errors.append(f"Unknown action: {action}")
                failure_count = len(check_ids)

        except Exception as e:
            logger.error(
                "Failed to bulk action checks",
                extra={"action": action},
                exc_info=True,
            )
            errors.append(str(e))
            failure_count = len(check_ids)

        return {
            "success_count": success_count,
            "failure_count": failure_count,
            "errors": errors,
        }

    @staticmethod
    async def bulk_modify(
        db: AsyncSession,
        check_ids: list[UUID],
        update_data: CheckUpdate,
        new_agent_id: UUID | None = None,
    ) -> dict:
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

        for check_id in check_ids:
            try:
                check = await CheckCoreService.get_check_by_id(db, check_id)

                # If changing agent, update the agent_id to preserve historical data
                if new_agent_id and new_agent_id != check.agent_id:
                    # Verify new agent exists
                    new_agent = await AgentCoreService.get_agent_by_id(db, new_agent_id)
                    if not new_agent:
                        raise ValueError(f"Target agent {new_agent_id} not found")

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
        requests: list,  # List of BulkCheckCreateRequest
    ) -> dict:
        """
        Bulk create checks from a list of URLs.

        Args:
            db: Database session
            agent_id: Agent UUID or ID
            requests: List of BulkCheckCreateRequest objects

        Returns:
            Dict with bulk creation results:
            {
                "total": int,
                "succeeded": int,
                "failed": int,
                "results": [BulkCheckResult, ...]
            }
        """

        results = []
        succeeded = 0
        failed = 0

        for req in requests:
            try:
                # Parse URL to detect check type
                url = req.url.strip()
                parsed = urlparse(url)

                # Detect check type from scheme
                if parsed.scheme in ("http", "https"):
                    check_type = "http"  # Check type is "http" for both http:// and https://
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
                    {
                        "url": url,
                        "status": "success",
                        "check_id": check.id,
                        "display_name": display_name,
                        "check_type": check_type,
                        "error": None,
                    }
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
                    {
                        "url": req.url,
                        "status": "failed",
                        "check_id": None,
                        "display_name": req.display_name,
                        "check_type": None,
                        "error": str(e),
                    }
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
        return {
            "total": len(requests),
            "succeeded": succeeded,
            "failed": failed,
            "results": results,
        }

    @staticmethod
    async def bulk_preview_checks(urls: list[str]) -> list[dict]:
        """
        Preview and validate bulk check URLs.

        Args:
            urls: List of URL strings to preview

        Returns:
            List of dicts with url, generated_name, check_type, validation_status, etc.
        """

        async def validate_url(url_str: str) -> dict:
            """Validate a single URL by fetching it."""
            try:
                # Parse URL to detect check type
                parsed = urlparse(url_str)

                # Detect check type from scheme
                if parsed.scheme in ("http", "https"):
                    check_type = "http"  # Check type is "http" for both http:// and https://
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
                    try:
                        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
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
                    except httpx.ConnectError:
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
    async def assign_alerts(db: AsyncSession, check_id: UUID, alert_ids: list[UUID]) -> None:
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

        # Add check to each alert
        for alert_id in alert_ids:
            try:
                await AlertCoreService.add_check(db, alert_id, check_id)
            except Exception:
                logger.warning(
                    "Failed to add check to alert",
                    extra={"check_id": str(check_id), "alert_id": str(alert_id)},
                    exc_info=True,
                )

        logger.info(
            "Assigned check to alerts",
            extra={"check_id": str(check_id), "alert_count": len(alert_ids)},
        )

    @staticmethod
    def build_check_list_response(checks: list, agent_name: str | None = None) -> CheckListResponse:
        """
        Build CheckListResponse from list of Check models.

        Transforms Check models into CheckResponse schemas with counts.
        Unpacks check_config JSONB fields and builds fully qualified names.

        Args:
            checks: List of Check model instances
            agent_name: Optional agent name for FQN building

        Returns:
            Dict ready for CheckListResponse validation containing:
            - checks: List of CheckResponse dicts
            - total: Total count
            - enabled_count: Count of enabled checks
            - disabled_count: Count of disabled checks
        """

        check_responses = []
        enabled_count = 0
        disabled_count = 0

        for check in checks:
            if check.enabled:
                enabled_count += 1
            else:
                disabled_count += 1

            # Build fully qualified name
            agent_part = agent_name or str(check.agent_id)
            fqn = f"{agent_part}:{check.display_name}"

            # Build base check dict
            check_dict = {
                "id": check.id,
                "agent_id": check.agent_id,
                "display_name": check.display_name,
                "check_type": check.check_type,
                "target": check.target,
                "description": check.description,
                "interval_seconds": check.interval_seconds,
                "timeout_seconds": check.timeout_seconds,
                "enabled": check.enabled,
                "retry_attempts": check.retry_attempts,
                "tags": check.tags,
                "script_code": check.script_code,
                "created_at": check.created_at,
                "updated_at": check.updated_at,
                "fully_qualified_name": fqn,
                "latest_status": None,
                "latest_latency_ms": None,
                "success_rate_24h": None,
            }

            # Unpack check_config JSONB fields into the check dict
            # This makes all check-type-specific fields available
            if check.check_config:
                check_dict.update(check.check_config)

            check_responses.append(CheckResponse.model_validate(check_dict))

        return CheckListResponse(
            checks=check_responses,
            total=len(check_responses),
            enabled_count=enabled_count,
            disabled_count=disabled_count,
        )

    @staticmethod
    async def create_check_with_alerts(
        db: AsyncSession,
        agent_id: UUID,
        data: CheckCreate,
        alert_ids: list[UUID],
        *,
        actor_is_admin: bool = False,
    ) -> Check:
        """Create a check and assign the supplied alerts in one workflow."""
        check = await CheckCoreService.create_check(
            db, agent_id, data, actor_is_admin=actor_is_admin
        )
        await CheckCoreService.assign_alerts(db, check.id, alert_ids)
        return check

    @staticmethod
    async def update_check_with_alerts(
        db: AsyncSession,
        check_id: UUID,
        data: CheckUpdate,
        alert_ids: list[UUID] | None = None,
        *,
        actor_is_admin: bool = False,
    ) -> Check:
        """Update a check and optionally sync the alert assignment list."""
        check = await CheckCoreService.update_check(
            db, check_id, data, actor_is_admin=actor_is_admin
        )
        if alert_ids is not None:
            await AlertCoreService.sync_check_alerts(db, check.id, alert_ids)
        return check

    @staticmethod
    async def clone_check_with_alerts(
        db: AsyncSession,
        source_check_id: UUID,
        target_agent_id: UUID,
        overrides: CheckCreate | None,
        alert_ids: list[UUID],
        *,
        actor_is_admin: bool = False,
    ) -> Check:
        """Clone a check and assign the supplied alerts in one workflow."""
        cloned_check = await CheckCoreService.clone_check(
            db, source_check_id, target_agent_id, overrides, actor_is_admin=actor_is_admin
        )
        await AlertCoreService.sync_check_alerts(db, cloned_check.id, alert_ids)
        return cloned_check

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
