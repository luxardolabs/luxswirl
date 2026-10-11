"""
Check API core service - checks in their JSON API shape.

One builder makes every CheckResponse, so a single check and the same check in
an agent's list carry the same fields (the check-type config included). Every
operation addressed by /agents/{agent_id}/checks/{check_id} is bound to that
agent: a check under another agent is not found here (LUXSWIRL-190 L-5).
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CheckNotFoundException
from app.models.check_model import Check
from app.schemas.check_schema import (
    BulkCheckCreateRequest,
    BulkCheckCreateResponse,
    CheckCreate,
    CheckListResponse,
    CheckResponse,
    CheckUpdate,
    CheckView,
)
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService


class CheckApiCoreService:
    """Checks for the JSON API: built once, bound to the agent in the path."""

    @staticmethod
    def to_response(check: Check, agent_label: str) -> CheckResponse:
        """`check` in its API shape; `agent_label` prefixes the qualified name.

        Every stored field is spelled out (assignment, dependency, retry and the
        check-type config), so the agent's wire contract is visible here and none
        falls back to a schema default.
        """
        return CheckResponse(
            id=check.id,
            agent_id=check.agent_id,
            display_name=check.display_name,
            check_type=check.check_type,
            target=check.target,
            description=check.description,
            interval_seconds=check.interval_seconds,
            timeout_seconds=check.timeout_seconds,
            enabled=check.enabled,
            expected_status=check.expected_status,
            http_method=check.http_method,
            verify_ssl=check.verify_ssl,
            json_path=check.json_path,
            expected_value=check.expected_value,
            record_type=check.record_type,
            nameserver=check.nameserver,
            port=check.port,
            expect_value=check.expect_value,
            connection_string=check.connection_string,
            query=check.query,
            retry_attempts=check.retry_attempts,
            retry_interval_seconds=check.retry_interval_seconds,
            resend_notification_after=check.resend_notification_after,
            tags=check.tags,
            script_code=check.script_code,
            depends_on_check_id=check.depends_on_check_id,
            assignment_mode=check.assignment_mode,
            agent_selector=check.agent_selector,
            created_at=check.created_at,
            updated_at=check.updated_at,
            fully_qualified_name=f"{agent_label}:{check.display_name}",
        )

    @staticmethod
    async def get_check_view(
        db: AsyncSession, check_id: UUID, *, include_script_code: bool = False
    ) -> CheckView:
        """One check as the web panels read it.

        Raises:
            CheckNotFoundException: If there is no such check
        """
        check = await CheckCoreService.get_check_by_id(
            db, check_id, include_script_code=include_script_code
        )
        if check is None:
            raise CheckNotFoundException("unknown", str(check_id))
        return CheckView.model_validate(check)

    @staticmethod
    async def list_potential_dependent_views(
        db: AsyncSession, parent_check_id: UUID
    ) -> list[CheckView]:
        """The checks that could depend on `parent_check_id`, for its panel."""
        return [
            CheckView.model_validate(check)
            for check in await CheckCoreService.list_potential_dependents(
                db, parent_check_id
            )
        ]

    @staticmethod
    async def list_all_check_views(db: AsyncSession) -> list[CheckView]:
        """Every check, for pickers that list them with their agent."""
        return [
            CheckView.model_validate(check)
            for check in await CheckCoreService.list_all_checks(db)
        ]

    @staticmethod
    def build_check_list_response(
        checks: list[Check], agent_name: str | None = None
    ) -> CheckListResponse:
        """An agent's checks as its check-list response, with counts."""
        responses = [
            CheckApiCoreService.to_response(check, agent_name or str(check.agent_id))
            for check in checks
        ]
        enabled = sum(1 for check in checks if check.enabled)
        return CheckListResponse(
            checks=responses,
            total=len(responses),
            enabled_count=enabled,
            disabled_count=len(responses) - enabled,
        )

    @staticmethod
    async def _agent_label(db: AsyncSession, agent_id: UUID) -> str:
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        return agent.agent_name or str(agent.id)

    @staticmethod
    async def _bound_check(db: AsyncSession, agent_id: UUID, check_id: UUID) -> Check:
        """The check, if it belongs to `agent_id`; otherwise not found."""
        check = await CheckCoreService.get_check_by_id(
            db, check_id, include_script_code=True
        )
        if check is None or check.agent_id != agent_id:
            raise CheckNotFoundException(str(agent_id), str(check_id))
        return check

    @staticmethod
    async def get_check(
        db: AsyncSession, agent_id: UUID, check_id: UUID
    ) -> CheckResponse:
        """One of the agent's checks.

        Raises:
            CheckNotFoundException: If the agent has no such check
            AgentNotFoundException: If the agent does not exist
        """
        check = await CheckApiCoreService._bound_check(db, agent_id, check_id)
        return CheckApiCoreService.to_response(
            check, await CheckApiCoreService._agent_label(db, agent_id)
        )

    @staticmethod
    async def create_check(
        db: AsyncSession, agent_id: UUID, data: CheckCreate
    ) -> CheckResponse:
        """Create a check on the agent.

        API Bearer tokens are admin-equivalent by design (see SECURITY.md), so
        synthetic checks are permitted on this path.

        Raises:
            AgentNotFoundException: If the agent does not exist
        """
        label = await CheckApiCoreService._agent_label(db, agent_id)
        check = await CheckCoreService.create_check(
            db, agent_id, data, actor_is_admin=True
        )
        return CheckApiCoreService.to_response(check, label)

    @staticmethod
    async def update_check(
        db: AsyncSession, agent_id: UUID, check_id: UUID, data: CheckUpdate
    ) -> CheckResponse:
        """Update one of the agent's checks.

        Raises:
            CheckNotFoundException: If the agent has no such check
        """
        await CheckApiCoreService._bound_check(db, agent_id, check_id)
        check = await CheckCoreService.update_check(
            db, check_id, data, actor_is_admin=True
        )
        return CheckApiCoreService.to_response(
            check, await CheckApiCoreService._agent_label(db, agent_id)
        )

    @staticmethod
    async def delete_check(db: AsyncSession, agent_id: UUID, check_id: UUID) -> None:
        """Delete one of the agent's checks.

        Raises:
            CheckNotFoundException: If the agent has no such check
        """
        await CheckApiCoreService._bound_check(db, agent_id, check_id)
        await CheckCoreService.delete_check(db, check_id)

    @staticmethod
    async def clone_check(
        db: AsyncSession,
        agent_id: UUID,
        check_id: UUID,
        overrides: CheckUpdate,
    ) -> CheckResponse:
        """Copy check `check_id` (from any agent) onto agent `agent_id`.

        Fields set in `overrides` replace the source's; the rest are copied.

        Raises:
            CheckNotFoundException: If the source check does not exist
            AgentNotFoundException: If the target agent does not exist
        """
        has_overrides = bool(overrides.model_dump(exclude_unset=True))
        check = await CheckCoreService.clone_check(
            db,
            check_id=check_id,
            agent_id=agent_id,
            overrides=overrides if has_overrides else None,
            actor_is_admin=True,
        )
        return CheckApiCoreService.to_response(
            check, await CheckApiCoreService._agent_label(db, agent_id)
        )

    @staticmethod
    async def bulk_create_checks(
        db: AsyncSession, agent_id: UUID, requests: list[BulkCheckCreateRequest]
    ) -> BulkCheckCreateResponse:
        """Create checks on the agent from URLs.

        Raises:
            AgentNotFoundException: If the agent does not exist
        """
        await AgentCoreService.get_agent_by_id(db, agent_id)
        return await CheckCoreService.bulk_create_checks(db, agent_id, requests)
