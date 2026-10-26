"""
Admin check core service - checks as the admin API serves them.

Each method takes the caller's role. Writes go through CheckCoreService, which
already refuses a synthetic check to a non-admin and every address the SSRF
policy refuses. A viewer is shown a check with no credential in it: the
userinfo of any URL in `target` or `connection_string` and any `password=` /
`pwd=` value are masked, and the synthetic `script_code` is left out.

A bulk action is judged whole before anything runs or is enqueued: an id no
check has is refused (422), and so is a non-admin's selection that names a
synthetic check (403). Delete and modify cascade or move, so they run as
maintenance jobs, which judge their owner again when they run.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credential_mask import mask_credentials
from app.core.exceptions import ValidationException
from app.core.synthetic_guard import SYNTHETIC, require_admin_actor
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind, UserRole
from app.schemas.admin_check_schema import (
    AdminBulkChecks,
    AdminBulkModify,
    AdminCheckClone,
    AdminCheckCreate,
    AdminCheckDependents,
    AdminCheckUpdate,
    BulkCheckResult,
    CheckPage,
    DependentsChange,
)
from app.schemas.check_schema import CheckResponse
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.schemas.pagination_schema import build_pagination
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_api_core_service import CheckApiCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_dependency_core_service import (
    CheckDependencyCoreService,
)
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService


class AdminCheckCoreService:
    """List, read, create, change, clone and delete checks for the admin API."""

    @staticmethod
    def _response(check: Check, role: UserRole) -> CheckResponse:
        response = CheckApiCoreService.to_response(
            check, check.agent.agent_name or str(check.agent_id)
        )
        if role == UserRole.VIEWER:
            return response.model_copy(
                update={
                    "target": mask_credentials(response.target),
                    "connection_string": mask_credentials(response.connection_string),
                    "script_code": None,
                }
            )
        return response

    @staticmethod
    async def _reloaded(
        db: AsyncSession, check_id: UUID, role: UserRole
    ) -> CheckResponse:
        check = await CheckCoreService.get_check_by_id(
            db, check_id, include_script_code=True
        )
        return AdminCheckCoreService._response(check, role)

    @staticmethod
    async def list_checks(
        db: AsyncSession,
        role: UserRole,
        *,
        page: int,
        per_page: int,
        agent_id: UUID | None = None,
        check_type: str | None = None,
        enabled_only: bool = False,
        tag: str | None = None,
        search: str | None = None,
    ) -> CheckPage:
        """One page of checks, filtered."""
        page = max(1, page)
        checks, total = await CheckCoreService.list_checks(
            db,
            agent_id=agent_id,
            check_type=check_type,
            enabled_only=enabled_only,
            tag=tag,
            search=search,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        pagination = build_pagination(page=page, per_page=per_page, total=total)
        return CheckPage.of(
            [AdminCheckCoreService._response(check, role) for check in checks],
            pagination,
        )

    @staticmethod
    async def get_check(
        db: AsyncSession, role: UserRole, check_id: UUID
    ) -> CheckResponse:
        return await AdminCheckCoreService._reloaded(db, check_id, role)

    @staticmethod
    async def create_check(
        db: AsyncSession, role: UserRole, data: AdminCheckCreate
    ) -> CheckResponse:
        check = await CheckCoreService.create_check(
            db,
            data.agent_id,
            data.to_check_create(),
            actor_is_admin=role == UserRole.ADMIN,
        )
        return await AdminCheckCoreService._reloaded(db, check.id, role)

    @staticmethod
    async def update_check(
        db: AsyncSession, role: UserRole, check_id: UUID, data: AdminCheckUpdate
    ) -> CheckResponse:
        await CheckCoreService.update_check(
            db, check_id, data, actor_is_admin=role == UserRole.ADMIN
        )
        return await AdminCheckCoreService._reloaded(db, check_id, role)

    @staticmethod
    async def set_enabled(
        db: AsyncSession, role: UserRole, check_id: UUID, *, enabled: bool
    ) -> CheckResponse:
        return await AdminCheckCoreService.update_check(
            db, role, check_id, AdminCheckUpdate(enabled=enabled)
        )

    @staticmethod
    async def clone_check(
        db: AsyncSession, role: UserRole, check_id: UUID, data: AdminCheckClone
    ) -> CheckResponse:
        source = await CheckCoreService.get_check_by_id(db, check_id)
        clone = await CheckCoreService.clone_check(
            db,
            check_id,
            data.agent_id or source.agent_id,
            data.overrides(),
            actor_is_admin=role == UserRole.ADMIN,
        )
        return await AdminCheckCoreService._reloaded(db, clone.id, role)

    @staticmethod
    async def delete_check(db: AsyncSession, role: UserRole, check_id: UUID) -> None:
        await CheckCoreService.delete_check(
            db, check_id, actor_is_admin=role == UserRole.ADMIN
        )

    # ── Bulk ────────────────────────────────────────────────────────────────

    @staticmethod
    async def _judge_selection(
        db: AsyncSession, role: UserRole, check_ids: list[UUID]
    ) -> None:
        """Refuse the whole selection when any of it may not be acted on."""
        checks = await CheckCoreService.get_checks_by_ids(db, check_ids)
        found = {check.id for check in checks}
        missing = [check_id for check_id in check_ids if check_id not in found]
        if missing:
            more = f" (and {len(missing) - 1} more)" if len(missing) > 1 else ""
            raise ValidationException(f"No check has the id {missing[0]}{more}")
        if any(check.check_type == SYNTHETIC for check in checks):
            require_admin_actor(actor_is_admin=role == UserRole.ADMIN)

    @staticmethod
    async def bulk_set_enabled(
        db: AsyncSession, role: UserRole, data: AdminBulkChecks, *, enabled: bool
    ) -> BulkCheckResult:
        """Enable or disable the checks now: an in-place change, safe in a request."""
        ids = data.ids
        await AdminCheckCoreService._judge_selection(db, role, ids)
        result = await CheckCoreService.bulk_set_enabled(
            db, ids, enabled=enabled, actor_is_admin=role == UserRole.ADMIN
        )
        return BulkCheckResult(
            success_count=result["success_count"],
            failure_count=result["failure_count"],
            errors=result["errors"],
        )

    @staticmethod
    async def enqueue_bulk_delete(
        db: AsyncSession, role: UserRole, owner_id: UUID, data: AdminBulkChecks
    ) -> MaintenanceJobResponse:
        """Delete the checks and their results in the maintenance worker."""
        ids = data.ids
        await AdminCheckCoreService._judge_selection(db, role, ids)
        return await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.BULK_CHECK_DELETE,
            target_id=None,
            params={"check_ids": [str(check_id) for check_id in ids]},
            owner_id=owner_id,
        )

    @staticmethod
    async def enqueue_bulk_modify(
        db: AsyncSession, role: UserRole, owner_id: UUID, data: AdminBulkModify
    ) -> MaintenanceJobResponse:
        """Change, move or alert the checks in the maintenance worker."""
        ids = data.ids
        await AdminCheckCoreService._judge_selection(db, role, ids)
        if data.agent_id is not None:
            await CheckCoreService.get_admitted_agent(db, data.agent_id)
        if data.alert_id is not None:
            await AlertCoreService.get_alert_by_id(db, data.alert_id)
        return await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.BULK_CHECK_MODIFY,
            target_id=None,
            params={
                "check_ids": [str(check_id) for check_id in ids],
                "update_fields": data.update_fields().model_dump(exclude_unset=True),
                "new_agent_id": str(data.agent_id) if data.agent_id else None,
                "alert_id": str(data.alert_id) if data.alert_id else None,
            },
            owner_id=owner_id,
        )

    # ── Dependents ──────────────────────────────────────────────────────────

    @staticmethod
    async def list_dependents(
        db: AsyncSession, role: UserRole, check_id: UUID
    ) -> list[CheckResponse]:
        """The checks that depend on `check_id`."""
        await CheckCoreService.get_check_by_id(db, check_id)
        return [
            AdminCheckCoreService._response(check, role)
            for check in await CheckDependencyCoreService.list_dependents(db, check_id)
        ]

    @staticmethod
    async def set_dependents(
        db: AsyncSession, role: UserRole, check_id: UUID, data: AdminCheckDependents
    ) -> DependentsChange:
        """Make `data.dependent_ids` exactly the checks that depend on `check_id`."""
        await CheckCoreService.get_check_by_id(db, check_id)
        added, removed = await CheckDependencyCoreService.set_dependents(
            db,
            check_id,
            list(dict.fromkeys(data.dependent_ids)),
            actor_is_admin=role == UserRole.ADMIN,
        )
        return DependentsChange(added=added, removed=removed)
