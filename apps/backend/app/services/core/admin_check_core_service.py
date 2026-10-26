"""
Admin check core service - checks as the admin API serves them.

Each method takes the caller's role. Writes go through CheckCoreService, which
already refuses a synthetic check to a non-admin and every address the SSRF
policy refuses. A viewer is shown a check without what the web UI's edit form
holds back from them: the database password in `connection_string`, and the
synthetic `script_code`.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.check_model import Check
from app.models.enum_model import UserRole
from app.schemas.admin_check_schema import AdminCheckClone, AdminCheckCreate, CheckPage
from app.schemas.check_schema import CheckResponse, CheckUpdate
from app.schemas.pagination_schema import build_pagination
from app.services.core.check_api_core_service import CheckApiCoreService
from app.services.core.check_core_service import CheckCoreService
from app.shared.log_redaction import scrub_text


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
                    "connection_string": (
                        scrub_text(response.connection_string)
                        if response.connection_string
                        else None
                    ),
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
        include_internal: bool = False,
    ) -> CheckPage:
        """One page of checks, filtered; internal (agent self-monitoring) ones only on request."""
        page = max(1, page)
        checks, total = await CheckCoreService.list_checks(
            db,
            agent_id=agent_id,
            check_type=check_type,
            enabled_only=enabled_only,
            tag=tag,
            search=search,
            exclude_internal=not include_internal,
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
        db: AsyncSession, role: UserRole, check_id: UUID, data: CheckUpdate
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
            db, role, check_id, CheckUpdate(enabled=enabled)
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
    async def delete_check(db: AsyncSession, check_id: UUID) -> None:
        await CheckCoreService.delete_check(db, check_id)
