"""
Check dependency core service - which checks depend on which.

A check may depend on one parent check: while the parent is down, the
child's notifications are suppressed. Dependencies are single-level (a
parent has no parent of its own) and never self-referencing.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ValidationException,
)
from app.core.synthetic_guard import (
    SYNTHETIC,
    require_admin_actor,
)
from app.crud.check_crud import CheckCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.models.check_model import Check
from app.schemas.check_schema import (
    CheckInDB,
)
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.check_dependency")


class CheckDependencyCoreService:
    """Read and change the dependencies between checks."""

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
        *,
        actor_is_admin: bool,
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

        # Attaching or detaching a synthetic check writes its dependency, a
        # change to a synthetic check, which is an admin's alone.
        touched = [by_id[cid] for cid in to_add | to_remove]
        if any(c.check_type == SYNTHETIC for c in touched):
            require_admin_actor(actor_is_admin=actor_is_admin)

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
    async def validate_dependency(
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
    async def list_eligible_parent_rows(
        db: AsyncSession, exclude_check_id: UUID | None = None
    ) -> dict[str, list[CheckInDB]]:
        """Eligible parent checks, grouped by agent name.

        Grouped HERE because the agent name comes from `check.agent`, a
        relationship: the rows are eager-loaded inside this session, and a
        schema that carried the relationship would lazy-load it outside one.
        """
        grouped: dict[str, list[CheckInDB]] = {}
        for c in await CheckDependencyCoreService.list_eligible_parents(
            db, exclude_check_id=exclude_check_id
        ):
            agent_name = (
                c.agent.agent_name if c.agent and c.agent.agent_name else "unknown"
            )
            grouped.setdefault(agent_name, []).append(CheckInDB.model_validate(c))
        return grouped
