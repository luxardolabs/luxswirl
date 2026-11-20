"""
Check service - business logic for check operations.
"""

from collections.abc import Collection, Mapping, Sequence
from typing import Any
from urllib.parse import ParseResult, urlparse
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes

from app.core.check_config import keep_type_fields, pack_config, take_config_updates
from app.core.datetime_utils import utc_now
from app.core.error_summary import error_summary
from app.core.exceptions import (
    AgentNotFoundException,
    CheckBusyException,
    CheckNotFoundException,
    ValidationException,
)
from app.core.synthetic_guard import (
    SYNTHETIC,
    require_admin_actor,
    validate_script,
)
from app.crud.agent_crud import AgentCRUD
from app.crud.check_crud import CheckCRUD
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.enum_model import (
    UNADMITTED_AGENT_STATUSES,
    AssignmentMode,
    BulkCheckOutcome,
    CheckHealthStatus,
    CheckType,
)
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
from app.services.core._check_bulk_names import bulk_display_name, unused_name
from app.services.core._check_bulk_request import bulk_check_create
from app.services.core._check_bulk_result import bulk_failed, bulk_outcome, with_refused
from app.services.core._check_endpoints import (
    endpoint_state,
    vet_changed_endpoints,
    vet_check_endpoints,
)
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_dependency_core_service import (
    CheckDependencyCoreService,
)
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.check")


class CheckCoreService:
    """Service for check operations."""

    @staticmethod
    async def get_admitted_agent(db: AsyncSession, agent_id: UUID) -> Agent:
        """The agent, if it takes checks: the agents the web UI's picker offers.

        Raises:
            AgentNotFoundException: no such agent.
            ValidationException: it is awaiting approval, refused or archived.
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if agent.approval_status in UNADMITTED_AGENT_STATUSES:
            raise ValidationException(
                f"Agent {agent.agent_name} is {agent.approval_status}: it takes no checks"
            )
        return agent

    @staticmethod
    async def hold_check_names(db: AsyncSession, agent_id: UUID) -> None:
        """Hold the agent's check names to the transaction's end, and judge
        that it still takes checks: what every writer that reads an agent's
        names to choose new ones takes first.

        A second such writer for the agent waits for this transaction, then
        reads the names it made. The agent's row is read FOR KEY SHARE, so an
        archive, reject or delete in flight is waited for and then seen, and
        one started later waits for this transaction (an agent delete takes
        the same hold, then judges the checks this one made); the agent's own
        reports and heartbeats go on meanwhile.

        Raises:
            AgentNotFoundException: no such agent.
            ValidationException: it is awaiting approval, refused or archived.
            CheckBusyException: a request's wait for the names ran out (an
                import holds them for its run).
        """
        if not await CheckCRUD.hold_names(db, agent_id):
            raise CheckBusyException()
        admission = await AgentCRUD.key_share_admission(db, agent_id)
        if admission is None:
            raise AgentNotFoundException(str(agent_id))
        status, _key_hash = admission
        if status in UNADMITTED_AGENT_STATUSES:
            raise ValidationException(f"The agent is {status}: it takes no checks")

    @staticmethod
    async def mark_checks_changed(db: AsyncSession, agent_id: UUID) -> None:
        """Stamp the agent's checks as changed, so it reloads them at its next
        heartbeat. A writer of many checks stamps once, after the last: the
        stamp is an update of the agent's row, which its heartbeat waits for
        until the transaction ends."""
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        agent.checks_updated_at = utc_now()
        attributes.flag_modified(agent, "checks_updated_at")

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
    def _require_admin_for_synthetic(
        check: Check, update_data: Mapping[str, Any], *, actor_is_admin: bool
    ) -> None:
        """Authz (OWASP API5): a synthetic check runs arbitrary code on an agent
        host, so turning a check into one, or changing one in any way (its
        script, its target, which agents run it, whether it runs), is an
        admin's alone.

        Raises:
            AuthorizationException: The check is or becomes synthetic and the
                actor is not an admin.
        """
        check_type = update_data.get("check_type", check.check_type)
        if SYNTHETIC in (check.check_type, check_type):
            require_admin_actor(actor_is_admin=actor_is_admin)

    @staticmethod
    async def _get_check_to_change(
        db: AsyncSession,
        check_id: UUID,
        *,
        include_script_code: bool = False,
        for_delete: bool = False,
        with_ids: Collection[UUID] = (),
        dependency_ids: Collection[UUID] = (),
    ) -> Check:
        """The check as committed, under its agent's synthetic guard and its row
        held to the transaction's end, for a write that judges it (a synthetic
        check is an admin's): a write making it synthetic in flight is waited
        for, none starts before this transaction ends, and the check cannot
        move to another agent between this judgement and the write. `with_ids`
        (a parent the write points the check at) are held FOR KEY SHARE, what
        the write's foreign key reads, in the same id-ordered pass;
        `dependency_ids` (the check and that parent) take the dependency lock
        between the guard and the rows.

        Raises:
            CheckNotFoundException: No such check.
            CheckBusyException: It moved to another agent while the guard was
                taken, or a request's wait for the guard or the rows ran out;
                nothing was changed.
        """
        # The agent id as a value: the reload below refreshes the session's one
        # copy of the check in place, so its attribute would read the new agent.
        guarded_agent = (await CheckCoreService.get_check_by_id(db, check_id)).agent_id
        if not await CheckCRUD.hold_synthetic_guard(db, {guarded_agent}):
            raise CheckBusyException()
        if not await CheckCRUD.hold_dependency(db, dependency_ids):
            raise CheckBusyException()
        locked = await CheckCRUD.hold_rows(
            db, [check_id], for_delete=for_delete, key_share_ids=with_ids
        )
        if locked is None:
            raise CheckBusyException()
        if check_id not in locked:
            raise CheckNotFoundException("unknown", str(check_id))
        if locked[check_id].agent_id != guarded_agent:
            raise CheckBusyException()
        if not include_script_code:
            return locked[check_id]
        # The row is held, so this reload reads the same version, script included.
        held = await CheckCRUD.get_as_stored(db, check_id, include_script_code=True)
        if not held:
            raise CheckNotFoundException("unknown", str(check_id))
        return held

    @staticmethod
    async def get_nameserver(db: AsyncSession, check_id: UUID) -> str | None:
        """The check's DNS nameserver, or None (no such check, or none set).

        A reader who does not hold the check's credentials is shown its results'
        errors with this nameserver cut to its origin (`masked_error`).
        """
        check = await CheckCRUD.get_by_id(db, check_id)
        return check.nameserver if check else None

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
        # SSRF protection: every address the check connects to, looked up
        # before anything is held.
        await vet_check_endpoints(
            db, data.check_type, data.target, data.nameserver, data.connection_string
        )
        if data.check_type == SYNTHETIC:
            require_admin_actor(actor_is_admin=actor_is_admin)
            # Made under the agent's synthetic guard, before any row is held:
            # a delete of the agent judging its checks waits for it.
            if not await CheckCRUD.hold_synthetic_guard(db, {agent_id}):
                raise CheckBusyException()
        agent = await CheckCoreService.get_admitted_agent(db, agent_id)

        parent_id = getattr(data, "depends_on_check_id", None)
        # Judged under the parent's dependency lock, so a write making the
        # parent a child meanwhile is waited for and then seen.
        if parent_id is not None and not await CheckCRUD.hold_dependency(
            db, {parent_id}
        ):
            raise CheckBusyException()
        await CheckDependencyCoreService.validate_dependency(
            db, parent_id, own_check_id=None
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
            # Only a synthetic check keeps a script, so none is stored unvalidated.
            script_code=data.script_code if data.check_type == SYNTHETIC else None,
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
        5. Copies alert assignments from source, each with its state
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
        clone_data = await CheckCoreService._merge_clone_data(db, source, overrides)

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
        # notified about it, so a failure rolls the clone back rather than
        # producing a half-wired one. Each mapping keeps the source's enabled
        # state and overrides, the global alerts create_check gave the clone
        # included; a snooze is not copied.
        await AlertCoreService.copy_check_mappings(db, check_id, cloned_check.id)

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
    async def _merge_clone_data(
        db: AsyncSession,
        source: Check,
        overrides: CheckCreate | CheckUpdate | None,
    ) -> CheckCreate:
        """The clone's create payload: the source check, with the caller's overrides.

        Only the fields the caller actually SET override the source; a schema
        default the caller never sent (timeout_seconds=10, enabled=True, ...)
        must not silently replace the source's value. None also keeps the source.
        """
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

        return CheckCreate(
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
            depends_on_check_id=override_fields.get(
                "depends_on_check_id", source.depends_on_check_id
            ),
        )

    @staticmethod
    async def update_check(
        db: AsyncSession,
        check_id: UUID,
        data: CheckUpdate,
        *,
        actor_is_admin: bool = False,
        skip_config_update: bool = False,
        guarded: bool = True,
        on_agent: UUID | None = None,
    ) -> Check:
        """Update a check. `skip_config_update` leaves the agent's
        checks_updated_at to the caller (`mark_checks_changed`); `guarded=False`
        leaves the synthetic guard to a caller that judged the check on its
        row, locked, or holds the guard already (bulk modify, an import).
        `on_agent` (with `guarded=False`) is the agent whose guard or names the
        caller holds: the check's row is locked and judged as stored, and the
        check must still be that agent's.

        Raises:
            CheckBusyException: The check moved to another agent, or its
                endpoints changed, while its lookups ran; nothing was changed.
        """
        update_data = data.model_dump(exclude_unset=True)
        # Refused, and its endpoints looked up (slow), before the guard is
        # taken; then judged again on the check as committed under it.
        check = await CheckCoreService.get_check_by_id(
            db, check_id, include_script_code=True
        )
        CheckCoreService._require_admin_for_synthetic(
            check, update_data, actor_is_admin=actor_is_admin
        )
        await vet_changed_endpoints(db, check, update_data)
        vetted = endpoint_state(check)
        # A parent the write newly points the check at is held with it, in id
        # order, FOR KEY SHARE: the write's foreign key reads it. One it already
        # points at is not read, so it is not held (a delete of it then waits
        # for nothing).
        new_parent = update_data.get("depends_on_check_id")
        parent_ids = (
            [new_parent]
            if new_parent is not None and new_parent != check.depends_on_check_id
            else []
        )
        if guarded:
            check = await CheckCoreService._get_check_to_change(
                db,
                check_id,
                include_script_code=True,
                with_ids=parent_ids,
                dependency_ids=[check_id, *parent_ids] if parent_ids else (),
            )
            CheckCoreService._require_admin_for_synthetic(
                check, update_data, actor_is_admin=actor_is_admin
            )
        elif on_agent is not None:
            # A check another write moved off the caller's agent meanwhile is
            # no longer under what the caller holds.
            if parent_ids and not await CheckCRUD.hold_dependency(
                db, [check_id, *parent_ids]
            ):
                raise CheckBusyException()
            held = await CheckCRUD.hold_rows(db, [check_id], key_share_ids=parent_ids)
            if not held or check_id not in held or held[check_id].agent_id != on_agent:
                raise CheckBusyException()
            check = held[check_id]
            CheckCoreService._require_admin_for_synthetic(
                check, update_data, actor_is_admin=actor_is_admin
            )
        if endpoint_state(check) != vetted or (
            new_parent is not None
            and new_parent != check.depends_on_check_id
            and new_parent not in parent_ids
        ):
            # Its endpoints, or the parent it points at, changed meanwhile.
            raise CheckBusyException()

        if "depends_on_check_id" in update_data:
            await CheckDependencyCoreService.validate_dependency(
                db, update_data["depends_on_check_id"], own_check_id=check.id
            )

        # The check type after this update, which decides which config fields apply.
        check_type = update_data.get("check_type", check.check_type)

        CheckCoreService._require_admin_for_synthetic(
            check, update_data, actor_is_admin=actor_is_admin
        )
        if check_type != SYNTHETIC:
            # Only a synthetic check keeps a script.
            update_data["script_code"] = None

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

        type_changes = check_type != check.check_type
        if config_updates or type_changes:
            # Merge into existing check_config; a new type drops the old one's fields.
            current_config = dict(check.check_config or {})
            if type_changes:
                current_config = keep_type_fields(current_config, check_type)
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

        # A script is validated when it is set, and when a check becomes synthetic
        # with the script it already holds. A change that leaves a synthetic
        # check's script alone (disabling it, say) does not re-judge it.
        script = update_data.get("script_code", check.script_code)
        script_now_runs = "script_code" in update_data or check.check_type != SYNTHETIC
        if check_type == SYNTHETIC and script and script_now_runs:
            validate_script(
                script,
                action="UPDATE",
                check_id=check_id,
                check_name=check.display_name,
            )

        # Update direct model fields
        for field, value in update_data.items():
            setattr(check, field, value)

        check.updated_at = utc_now()

        # Update agent's checks_updated_at timestamp to trigger config reload
        if not skip_config_update:
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
            skip=offset,
            limit=limit,
        )

    @staticmethod
    async def list_checks_with_health(
        db: AsyncSession,
        *,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        enabled_only: bool = False,
        tag: str | None = None,
        search: str | None = None,
        search_full_target: bool = False,
        status: CheckHealthStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[tuple[Check, bool | None, float | None]], int]:
        """`list_checks`' page, each check with its current result's success and
        latency, read in the statement that filters on `status` (that health)."""
        return await CheckCRUD.list_page_with_health(
            db,
            agent_id=agent_id,
            check_type=check_type,
            enabled_only=enabled_only,
            tag=tag,
            search=search,
            search_full_target=search_full_target,
            status=status,
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
            limit=limit,
            offset=offset,
        )
        return [CheckInDB.model_validate(c) for c in checks], total

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

        list_checks_for_agent stays ORM: the job-to-check and
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
    async def delete_check(
        db: AsyncSession, check_id: UUID, *, actor_is_admin: bool = False
    ) -> None:
        """Delete a check; a synthetic one only for an admin, judged under its
        agent's synthetic guard."""
        check = await CheckCoreService._get_check_to_change(
            db, check_id, for_delete=True
        )
        if check.check_type == SYNTHETIC:
            require_admin_actor(actor_is_admin=actor_is_admin)

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
    async def _bulk_permitted(
        db: AsyncSession,
        check_ids: list[UUID],
        *,
        actor_is_admin: bool,
        for_delete: bool = False,
    ) -> tuple[list[UUID], list[str]]:
        """The ids an actor may change in bulk, and one error per id refused.

        A synthetic check is an admin's to change in any way (`update_check`),
        so a non-admin's bulk action leaves each one alone and reports it. A
        bulk job runs long, so it holds no synthetic guard: the checks it may
        change are held in id order (FOR UPDATE for a delete, else FOR NO KEY
        UPDATE) and judged again as stored, so one made synthetic in flight is
        refused too, and a check refused at first is never held.
        """
        if actor_is_admin or not check_ids:
            return check_ids, []
        synthetic = {
            check.id
            for check in await CheckCRUD.get_with_agent_by_ids(db, check_ids)
            if check.check_type == SYNTHETIC
        }
        synthetic |= {
            check.id
            for check in await CheckCRUD.get_locked_by_ids(
                db,
                [check_id for check_id in check_ids if check_id not in synthetic],
                for_delete=for_delete,
            )
            if check.check_type == SYNTHETIC
        }
        return (
            [check_id for check_id in check_ids if check_id not in synthetic],
            [
                f"Check {check_id}: synthetic checks require admin privileges"
                for check_id in check_ids
                if check_id in synthetic
            ],
        )

    @staticmethod
    async def bulk_set_enabled(
        db: AsyncSession, check_ids: list[UUID], *, enabled: bool, actor_is_admin: bool
    ) -> dict[str, Any]:
        """Enable or disable `check_ids`: an in-place UPDATE, safe in a request.

        A non-admin's synthetic checks are left alone and counted as failures.

        Returns:
            {"success_count", "failure_count", "errors"}
        """
        asked = check_ids
        # swallowed-exceptions: a DB failure (a lock waited on past the timeout included) is reported as failures; the savepoint keeps the caller's session committable, which is what delivers that report
        try:
            async with db.begin_nested():
                check_ids, refused = await CheckCoreService._bulk_permitted(
                    db, check_ids, actor_is_admin=actor_is_admin
                )
                success_count = await CheckCRUD.bulk_set_enabled(db, check_ids, enabled)
                await CheckCoreService._touch_agents_of(db, check_ids)
        except SQLAlchemyError as e:
            logger.exception("Failed to bulk-toggle checks", extra={"enabled": enabled})
            return bulk_failed(asked, e)
        logger.info(
            "Bulk toggle complete",
            extra={"enabled": enabled, "success_count": success_count},
        )
        return with_refused(bulk_outcome(check_ids, success_count), refused)

    @staticmethod
    async def bulk_delete(
        db: AsyncSession, check_ids: list[UUID], *, actor_is_admin: bool
    ) -> dict[str, Any]:
        """Delete `check_ids`, cascading through their results.

        The cascade is long, so this runs only in the maintenance worker (the
        bulk_check_delete job), never inside a web request. A non-admin's
        synthetic checks are left alone and counted as failures.

        Returns:
            {"success_count", "failure_count", "errors"}
        """
        asked = check_ids
        # swallowed-exceptions: a DB failure (a lock waited on past the timeout included) is reported as failures; the savepoint keeps the worker's session committable, which is what delivers that report
        try:
            async with db.begin_nested():
                check_ids, refused = await CheckCoreService._bulk_permitted(
                    db, check_ids, actor_is_admin=actor_is_admin, for_delete=True
                )
                # Locked in id order first, as a report locks them: the delete
                # below takes its row locks in scan order.
                await CheckCRUD.lock_for_delete(db, check_ids=check_ids)
                affected = await CheckCoreService._touch_agents_of(db, check_ids)
                success_count = await CheckCRUD.bulk_delete_by_ids(db, check_ids)
        except SQLAlchemyError as e:
            logger.exception("Failed to bulk-delete checks")
            return bulk_failed(asked, e)
        logger.info(
            "Bulk deleted checks",
            extra={"success_count": success_count, "affected_agent_count": affected},
        )
        return with_refused(bulk_outcome(check_ids, success_count), refused)

    @staticmethod
    async def bulk_modify(
        db: AsyncSession,
        check_ids: list[UUID],
        update_data: CheckUpdate,
        new_agent_id: UUID | None = None,
        *,
        actor_is_admin: bool,
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

        # A check listed twice is one check, changed once.
        check_ids = list(dict.fromkeys(check_ids))
        success_count = 0
        failure_count = 0
        errors = []
        changed: list[UUID] = []
        affected_agents = set()  # Track agents that need config_version update

        # The selection read once, unheld. An admin's change that leaves checks
        # synthetic on an agent (a move of synthetic checks, a retype to
        # synthetic) then holds those agents' synthetic guards; the checks it
        # may change are held in id order and judged again as stored, so one
        # made synthetic, or moved, in flight is refused below (a non-admin's
        # synthetic checks are refused unheld). A check id that resolves to
        # nothing fails on its own below.
        checks_by_id = {
            check.id: check
            for check in await CheckCRUD.get_checks_by_ids(db, check_ids)
        }
        guarded: set[UUID] = set()
        if actor_is_admin and update_data.check_type == SYNTHETIC:
            guarded = (
                {new_agent_id}
                if new_agent_id
                else {check.agent_id for check in checks_by_id.values()}
            )
        elif (
            actor_is_admin
            and new_agent_id
            and any(check.check_type == SYNTHETIC for check in checks_by_id.values())
        ):
            guarded = {new_agent_id}
        if not await CheckCRUD.hold_synthetic_guard(db, guarded):
            raise CheckBusyException()
        await CheckCRUD.get_locked_by_ids(
            db,
            [
                check.id
                for check in checks_by_id.values()
                if actor_is_admin or check.check_type != SYNTHETIC
            ],
        )
        # get_admitted_agent RAISES when the agent is gone or takes no checks
        # (pending, rejected, archived), so resolve it here and keep the refusal
        # as a per-check failure — the loop records one failure per check for a
        # bad target, and callers count on that shape.
        new_agent = None
        new_agent_error: str | None = None
        if new_agent_id:
            try:
                new_agent = await CheckCoreService.get_admitted_agent(db, new_agent_id)
            except AgentNotFoundException:
                new_agent_error = f"Target agent {new_agent_id} not found"
            except ValidationException as e:
                new_agent_error = e.message

        for check_id in check_ids:
            try:
                check = checks_by_id.get(check_id)
                if not check:
                    raise CheckNotFoundException("unknown", str(check_id))

                moving = bool(new_agent_id) and new_agent_id != check.agent_id
                lands_on = new_agent_id if moving else check.agent_id
                makes_synthetic = (
                    update_data.check_type == SYNTHETIC
                    and check.check_type != SYNTHETIC
                ) or (
                    moving and SYNTHETIC in (check.check_type, update_data.check_type)
                )
                if actor_is_admin and makes_synthetic and lands_on not in guarded:
                    # It would become synthetic on an agent whose guard this
                    # does not hold (it was retyped or moved meanwhile).
                    raise CheckBusyException()

                # If changing agent, update the agent_id to preserve historical data
                if moving:
                    if not new_agent:
                        raise ValueError(new_agent_error or "Target agent not found")

                    # Moving a synthetic check runs its script somewhere new.
                    if check.check_type == SYNTHETIC:
                        require_admin_actor(actor_is_admin=actor_is_admin)
                    # Track both old and new agents for config sync
                    affected_agents.add(check.agent)
                    affected_agents.add(new_agent)
                    CheckCoreService._move_check_to_agent(check, new_agent, update_data)
                else:
                    # Updated in place; its agent is stamped once, after the
                    # last check, so the agent's row is not held for the run.
                    await CheckCoreService.update_check(
                        db,
                        check.id,
                        update_data,
                        actor_is_admin=actor_is_admin,
                        guarded=False,
                        skip_config_update=True,
                    )
                    affected_agents.add(check.agent)

                success_count += 1
                changed.append(check_id)

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
            "changed_ids": changed,
        }

    @staticmethod
    def _move_check_to_agent(
        check: Check, new_agent: Agent, update_data: CheckUpdate
    ) -> None:
        """Reassign `check` to `new_agent`, applying the bulk-modify fields with it.

        The agent_id changes in place, so the check keeps its id and with it all
        of its historical results.
        """
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

    @staticmethod
    async def bulk_create_checks(
        db: AsyncSession,
        agent_id: UUID,
        requests: list[BulkCheckCreateRequest],
    ) -> BulkCheckCreateResponse:
        """Create a check on the agent for each URL in `requests`, each in its
        own savepoint, named so no two of the agent's checks share a name.

        Returns: the counts, and each URL's outcome (its check, or its error).
        """
        results: list[BulkCheckResult] = []
        succeeded = 0
        failed = 0
        # A second bulk create or import for the agent waits for this one's
        # end, then reads the names it made.
        await CheckCoreService.hold_check_names(db, agent_id)
        taken = set(await CheckCRUD.display_names_for_agent(db, agent_id))
        numbers: dict[str, int] = {}

        for req in requests:
            try:
                check_type, url, parsed = CheckCoreService.classify_bulk_url(
                    req.url.strip()
                )
                # A given name too: the web form posts the name its preview made.
                display_name = unused_name(
                    req.display_name or bulk_display_name(parsed), taken, numbers
                )

                check_data = bulk_check_create(req, check_type, url, display_name)

                # Each in its own savepoint: one that fails leaves the rest to commit.
                async with db.begin_nested():
                    check = await CheckCoreService.create_check(
                        db, agent_id, check_data, skip_config_update=True
                    )
                taken.add(display_name)

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
                        error=error_summary(e),
                    )
                )
                failed += 1

        if succeeded:
            await CheckCoreService.mark_checks_changed(db, agent_id)
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
    def classify_bulk_url(url: str) -> tuple[str, str, ParseResult]:
        """A bulk-import URL's check type, the URL to monitor, and its parts.

        The check type comes from the scheme ("http" for both http:// and
        https://); a URL with no scheme is taken as https.
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme in ("http", "https"):
                return "http", url, parsed
            if not parsed.scheme:
                full_url = f"https://{url}"
                return "http", full_url, urlparse(full_url)
        except ValueError:
            # The parser's own message quotes what it refused.
            raise ValueError("The URL could not be parsed") from None
        try:
            CheckType(parsed.scheme)
        except ValueError:
            # What reads as a scheme can be a user (`user:pw@db`), and the error quotes it.
            raise ValueError("The URL's scheme is no check type") from None
        return parsed.scheme, url, parsed  # tcp, etc.

    @staticmethod
    def bulk_display_name(parsed: ParseResult) -> str:
        """The name a bulk-imported check gets when none is given."""
        return bulk_display_name(parsed)

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
        # swallowing it would return success while the check was wired to
        # fewer alerts than requested.
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
