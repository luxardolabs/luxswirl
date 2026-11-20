"""
Check transfer core service - an agent's checks exported, imported, or made in
bulk from URLs.

Import and bulk create can be thousands of inserts, so the request judges what
it can (the agent, the alerts, the size) and queues a maintenance job; the job
re-judges its owner when it runs. A job is keyed by what it asks for: two
different imports for one agent are two jobs, and the same one sent twice
while the first is queued is one.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_summary import error_summary
from app.core.exceptions import (
    AgentNotFoundException,
    NotFoundException,
    ValidationException,
)
from app.core.synthetic_guard import SYNTHETIC
from app.crud.check_crud import CheckCRUD
from app.models.check_model import Check
from app.models.enum_model import ImportMode, MaintenanceJobKind
from app.schemas.check_schema import (
    BulkCheckCreateRequest,
    CheckCreate,
    CheckInDB,
    CheckUpdate,
)
from app.schemas.import_export_schema import CheckExport, CheckExportDocument
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.check_transfer")

# The most checks one import or bulk create takes.
TRANSFER_MAX = 10_000

# Names a job by what it asks for, so the same request sent twice while the
# first is queued (a double click, a client's retry) is one job.
_REQUEST_KEYS = UUID("6c0c3b0e-3f69-4f0e-9a39-6f1f7b8f2a51")


def _request_key(
    kind: MaintenanceJobKind, owner_id: UUID, params: dict[str, Any]
) -> UUID:
    body = json.dumps(
        {"kind": kind.value, "owner": str(owner_id), "params": params}, sort_keys=True
    )
    return uuid5(_REQUEST_KEYS, body)


def _or(value: int | None, default: int) -> int:
    """`value`, or `default` when it is unset (a 0 is kept)."""
    return default if value is None else value


def _entry(check: CheckInDB, names: dict[UUID, str]) -> CheckExport:
    """One check as the export carries it; `names` are its agent's checks by id.

    A parent on the same agent is named, so an import onto another agent
    points at that agent's check of the name; a parent elsewhere is its id.
    """
    parent = check.depends_on_check_id
    parent_name = names.get(parent) if parent is not None else None
    return CheckExport(
        name=check.display_name,
        check_type=check.check_type,
        target=check.target,
        interval=_or(check.interval_seconds, 60),
        timeout=_or(check.timeout_seconds, 5),
        retry_attempts=_or(check.retry_attempts, 2),
        enabled=check.enabled,
        description=check.description,
        http_method=check.http_method,
        expected_status=check.expected_status,
        json_path=check.json_path,
        expected_value=check.expected_value,
        tags=check.tags,
        retry_interval_seconds=check.retry_interval_seconds,
        resend_notification_after=check.resend_notification_after,
        verify_ssl=check.verify_ssl,
        record_type=check.record_type,
        nameserver=check.nameserver,
        port=check.port,
        expect_value=check.expect_value,
        connection_string=check.connection_string,
        query=check.query,
        script_code=check.script_code,
        depends_on=parent_name,
        depends_on_check_id=parent if parent_name is None else None,
    )


class CheckTransferCoreService:
    """Export, import and bulk-create an agent's checks."""

    @staticmethod
    async def export_document(db: AsyncSession, agent_id: UUID) -> CheckExportDocument:
        """The agent's checks in the form an import takes back.

        Raises:
            AgentNotFoundException: If there is no such agent
        """
        agent = await AgentCoreService.get_agent(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))
        checks = await CheckCoreService.list_agent_checks(db, agent_id)
        names = {check.id: check.display_name for check in checks}
        return CheckExportDocument(
            agent_id=agent.id,
            agent_name=agent.agent_name,
            agent_hostname=agent.hostname,
            total_checks=len(checks),
            checks=[_entry(check, names) for check in checks],
        )

    @staticmethod
    async def enqueue_import(
        db: AsyncSession,
        owner_id: UUID,
        agent_id: UUID,
        mode: ImportMode,
        checks: list[dict[str, Any]],
    ) -> MaintenanceJobResponse:
        """Queue an import of `checks` onto the agent.

        Each entry is validated when the job runs, and one that is not a check
        is that entry's error. `replace` overwrites a check of the same name;
        `merge` skips it.

        Raises:
            AgentNotFoundException: If there is no such agent
            ValidationException: If there are no checks to import
        """
        if not checks:
            raise ValidationException("No checks to import")
        if len(checks) > TRANSFER_MAX:
            raise ValidationException(
                f"An import takes at most {TRANSFER_MAX:,} checks; this has {len(checks):,}"
            )
        await CheckCoreService.get_admitted_agent(db, agent_id)
        params = {"agent_id": str(agent_id), "mode": mode.value, "checks": checks}
        job = await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.BULK_CHECK_IMPORT,
            target_id=_request_key(
                MaintenanceJobKind.BULK_CHECK_IMPORT, owner_id, params
            ),
            params=params,
            owner_id=owner_id,
        )
        logger.info(
            "Enqueued bulk_check_import maintenance job",
            extra={
                "agent_id": str(agent_id),
                "check_count": len(checks),
                "mode": mode.value,
                "job_id": str(job.id),
            },
        )
        return job

    @staticmethod
    async def enqueue_import_checks(
        db: AsyncSession,
        owner_id: UUID,
        agent_id: UUID,
        mode: ImportMode,
        checks: list[CheckExport],
    ) -> MaintenanceJobResponse:
        """Queue an import of checks already held to the export's form."""
        return await CheckTransferCoreService.enqueue_import(
            db,
            owner_id,
            agent_id,
            mode,
            # Only what each entry named: an optional setting it left out is
            # left as the check has it, as for the web upload.
            [check.model_dump(mode="json", exclude_unset=True) for check in checks],
        )

    @staticmethod
    async def enqueue_bulk_create(
        db: AsyncSession,
        owner_id: UUID,
        agent_id: UUID,
        requests: Sequence[BulkCheckCreateRequest],
        alert_ids: list[UUID],
    ) -> MaintenanceJobResponse:
        """Queue one check per URL on the agent, each given the alerts.

        Raises:
            AgentNotFoundException: If there is no such agent
            ValidationException: If there are no URLs, the agent takes no
                checks, or an alert does not exist
        """
        if not requests:
            raise ValidationException("No URLs to create checks from")
        if len(requests) > TRANSFER_MAX:
            raise ValidationException(
                f"A bulk create takes at most {TRANSFER_MAX:,} URLs; this has {len(requests):,}"
            )
        await CheckCoreService.get_admitted_agent(db, agent_id)
        alerts = list(dict.fromkeys(alert_ids))
        for alert_id in alerts:
            try:
                await AlertCoreService.get_alert_by_id(db, alert_id)
            except NotFoundException:
                raise ValidationException(f"No alert has the id {alert_id}") from None
        params = {
            "agent_id": str(agent_id),
            "requests": [r.model_dump(mode="json") for r in requests],
            "alert_ids": [str(a) for a in alerts],
        }
        job = await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.BULK_CHECK_CREATE,
            target_id=_request_key(
                MaintenanceJobKind.BULK_CHECK_CREATE, owner_id, params
            ),
            params=params,
            owner_id=owner_id,
        )
        logger.info(
            "Enqueued bulk_check_create maintenance job",
            extra={
                "agent_id": str(agent_id),
                "count": len(requests),
                "job_id": str(job.id),
            },
        )
        return job

    @staticmethod
    async def bulk_import_checks(
        db: AsyncSession,
        agent_id: UUID,
        checks: Sequence[Mapping[str, Any]],
        overwrite: bool,
        *,
        actor_is_admin: bool,
    ) -> dict[str, Any]:
        """Import checks for an agent: the ONE import, run by the web upload's
        worker job.

        Each entry is validated on its own: one that is not a valid check (an
        unknown type or method from an older export) is an error for that check,
        never a refusal of the whole import. `overwrite` replaces an existing
        check of the same name, changing only the optional settings the entry
        names; otherwise it is skipped. `actor_is_admin` is the caller's
        authority, as for a single create: a synthetic check is refused unless
        it is True.

        Each entry runs in its own savepoint, so one that fails (even in the
        database) leaves the rest to commit. A parent an entry names is linked
        after every entry is made, in a savepoint of its own: one that cannot be
        is that entry's error, and its check stays. Builds the CheckCreate /
        CheckUpdate DTOs here, so routers don't marshal schemas. The agent must
        take checks. Returns {created, updated, skipped, errors}.
        """
        counts = {"created": 0, "updated": 0, "skipped": 0}
        errors: list[dict[str, str]] = []

        # A second import or bulk create for the agent waits for this one's
        # end, then reads the names it made. An agent that takes no checks
        # keeps the ones it has, too.
        await CheckCoreService.hold_check_names(db, agent_id)
        # An import that makes a synthetic check holds the agent's synthetic
        # guard from its start, before any entry's row is written.
        if any(raw.get("check_type") == SYNTHETIC for raw in checks):
            await CheckCRUD.hold_synthetic_guard(db, {agent_id})

        existing = await CheckCoreService.list_checks_for_agent(db, agent_id)
        existing_map = {c.display_name: c for c in existing}
        # Entries that name a parent, linked once every entry is made.
        to_link: list[CheckExport] = []

        for raw in checks:
            name = str(raw.get("name") or "unknown")
            # swallowed-exceptions: one entry's failure is that entry's error, kept for the caller; its savepoint keeps the rest of the import committable
            try:
                check_data = CheckExport.model_validate(raw)
                async with db.begin_nested():
                    outcome = await CheckTransferCoreService._import_one(
                        db,
                        agent_id,
                        check_data,
                        existing_map,
                        overwrite=overwrite,
                        actor_is_admin=actor_is_admin,
                    )
            except Exception as e:
                errors.append({"check": name, "error": error_summary(e)})
                logger.exception("Error importing check", extra={"check_name": name})
                continue
            counts[outcome] += 1
            if outcome != "skipped" and check_data.names_parent():
                to_link.append(check_data)

        # Unlinks first: a check the file frees of its parent may be another
        # entry's parent, whatever their order in the file.
        to_link.sort(
            key=lambda entry: (
                entry.depends_on is not None or entry.depends_on_check_id is not None
            )
        )
        for check_data in to_link:
            # swallowed-exceptions: a parent that cannot be linked is that entry's error, kept for the caller; the check itself is made, and its savepoint keeps the rest committable
            try:
                async with db.begin_nested():
                    await CheckTransferCoreService._link_parent(
                        db,
                        agent_id,
                        check_data,
                        existing_map,
                        actor_is_admin=actor_is_admin,
                    )
            except Exception as e:
                errors.append({"check": check_data.name, "error": error_summary(e)})
                logger.exception(
                    "Error linking an imported check to its parent",
                    extra={"check_name": check_data.name},
                )

        created, updated, skipped = (
            counts["created"],
            counts["updated"],
            counts["skipped"],
        )
        if created or updated:
            await CheckCoreService.mark_checks_changed(db, agent_id)
        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
        }

    @staticmethod
    async def _import_one(
        db: AsyncSession,
        agent_id: UUID,
        check_data: CheckExport,
        existing_map: dict[str, Check],
        *,
        overwrite: bool,
        actor_is_admin: bool,
    ) -> str:
        """Import one entry; "created", "updated" or "skipped".

        A check the import creates joins `existing_map`, so a name met again
        in the same import is skipped or overwritten, never made twice.
        """
        existing = existing_map.get(check_data.name)
        if existing is not None:
            if not overwrite:
                return "skipped"
            await CheckCoreService.update_check(
                db,
                existing.id,
                CheckUpdate.model_validate(check_data.settings()),
                actor_is_admin=actor_is_admin,
                skip_config_update=True,
                guarded=False,
                on_agent=agent_id,
            )
            return "updated"
        existing_map[check_data.name] = await CheckCoreService.create_check(
            db,
            agent_id,
            CheckCreate.model_validate(
                {"display_name": check_data.name, **check_data.settings()}
            ),
            skip_config_update=True,
            actor_is_admin=actor_is_admin,
        )
        return "created"

    @staticmethod
    async def _link_parent(
        db: AsyncSession,
        agent_id: UUID,
        check_data: CheckExport,
        existing_map: dict[str, Check],
        *,
        actor_is_admin: bool,
    ) -> None:
        """Point an imported check at the parent its entry names, or at none.

        `depends_on` is a check of the agent imported onto, by name; it runs
        after every entry is made, so a parent later in the file is found.

        Raises:
            ValidationException: The agent has no check of that name
        """
        parent_id = check_data.depends_on_check_id
        if check_data.depends_on is not None:
            parent = existing_map.get(check_data.depends_on)
            if parent is None:
                raise ValidationException(
                    f"It depends on {check_data.depends_on!r}, which this agent "
                    "does not have"
                )
            parent_id = parent.id
        check = existing_map[check_data.name]
        if check.depends_on_check_id != parent_id:
            await CheckCoreService.update_check(
                db,
                check.id,
                CheckUpdate(depends_on_check_id=parent_id),
                actor_is_admin=actor_is_admin,
                skip_config_update=True,
                guarded=False,
                on_agent=agent_id,
            )
