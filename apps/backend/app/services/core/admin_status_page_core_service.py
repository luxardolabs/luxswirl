"""
Admin status page core service - status pages as the admin API serves them.

Every check and agent a page's items name is proved to exist first, so an
unknown id is a 422 naming it, and a page never shows a check that is not
there. A page is deleted by a maintenance job, as in the web UI.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AgentNotFoundException, ValidationException
from app.models.enum_model import MaintenanceJobKind
from app.schemas.admin_status_page_schema import (
    AdminStatusPage,
    AdminStatusPageCreate,
    AdminStatusPageItems,
    AdminStatusPageUpdate,
    CheckItem,
    StatusPageItem,
    StatusPagePage,
)
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.schemas.pagination_schema import build_pagination
from app.schemas.status_page_schema import (
    ALLOWED_TIME_RANGES,
    StatusPageCreate,
    StatusPageInDB,
    StatusPageUpdate,
)
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.core.status_page_core_service import StatusPageCoreService

# What a page shows when its config names no window, as the public view does.
DEFAULT_STATUS_BAR_MINUTES = 30


def _stored_items(items: list[StatusPageItem]) -> list[dict[str, Any]]:
    """The items as the page's JSON column holds them, in order."""
    stored: list[dict[str, Any]] = []
    for order, item in enumerate(items):
        if isinstance(item, CheckItem):
            stored.append(
                {"type": "check", "check_id": str(item.check_id), "order": order}
            )
            continue
        group: dict[str, Any] = {
            "type": "group",
            "name": item.name,
            "order": order,
            "collapsed": item.collapsed,
            "sort_by": item.sort_by,
            "sort_direction": item.sort_direction,
        }
        if item.checks is not None:
            group["checks"] = [str(c) for c in dict.fromkeys(item.checks)]
        elif item.filter is not None:
            group["filter"] = item.filter.model_dump(mode="json", exclude_none=True)
        stored.append(group)
    return stored


def _response(page: StatusPageInDB) -> AdminStatusPage:
    minutes = (page.config or {}).get("status_bar_minutes")
    return AdminStatusPage(
        id=page.id,
        name=page.name,
        slug=page.slug,
        description=page.description,
        is_public=page.is_public,
        status_bar_minutes=(
            minutes if minutes in ALLOWED_TIME_RANGES else DEFAULT_STATUS_BAR_MINUTES
        ),
        items=page.items,
        created_at=page.created_at,
        updated_at=page.updated_at,
    )


class AdminStatusPageCoreService:
    """List, read, create, change and delete status pages, and their items."""

    @staticmethod
    async def _require_references(
        db: AsyncSession, items: list[StatusPageItem]
    ) -> None:
        """Every check and agent the items name exists."""
        check_ids: list[UUID] = []
        agent_ids: list[UUID] = []
        for item in items:
            if isinstance(item, CheckItem):
                check_ids.append(item.check_id)
            elif item.checks is not None:
                check_ids.extend(item.checks)
            elif item.filter is not None and item.filter.agent_id is not None:
                agent_ids.append(item.filter.agent_id)
        found = {c.id for c in await CheckCoreService.get_checks_by_ids(db, check_ids)}
        missing = [c for c in dict.fromkeys(check_ids) if c not in found]
        if missing:
            raise ValidationException(f"No check has the id {missing[0]}")
        for agent_id in dict.fromkeys(agent_ids):
            try:
                await AgentCoreService.get_agent_by_id(db, agent_id)
            except AgentNotFoundException:
                raise ValidationException(f"No agent has the id {agent_id}") from None

    @staticmethod
    async def get_page(db: AsyncSession, status_page_id: UUID) -> AdminStatusPage:
        return _response(
            await StatusPageCoreService.get_status_page(db, status_page_id)
        )

    @staticmethod
    async def list_pages(
        db: AsyncSession, *, page: int, per_page: int, is_public: bool | None
    ) -> StatusPagePage:
        """One page of status pages, by name."""
        page = max(1, page)
        pages, total = await StatusPageCoreService.list_status_pages(
            db, is_public=is_public, limit=per_page, offset=(page - 1) * per_page
        )
        return StatusPagePage.of(
            [_response(p) for p in pages],
            build_pagination(page=page, per_page=per_page, total=total),
        )

    @staticmethod
    async def create_page(
        db: AsyncSession, data: AdminStatusPageCreate
    ) -> AdminStatusPage:
        await AdminStatusPageCoreService._require_references(db, data.items)
        created = await StatusPageCoreService.create_status_page(
            db,
            StatusPageCreate(
                name=data.name,
                slug=data.slug,
                description=data.description,
                is_public=data.is_public,
                config={"status_bar_minutes": data.status_bar_minutes},
                items=_stored_items(data.items),
            ),
        )
        return _response(created)

    @staticmethod
    async def update_page(
        db: AsyncSession, status_page_id: UUID, data: AdminStatusPageUpdate
    ) -> AdminStatusPage:
        """Change the fields sent; the page's other config is kept."""
        sent = data.model_dump(exclude_unset=True)
        minutes = sent.pop("status_bar_minutes", None)
        if minutes is not None:
            existing = await StatusPageCoreService.get_status_page(db, status_page_id)
            sent["config"] = {**(existing.config or {}), "status_bar_minutes": minutes}
        updated = await StatusPageCoreService.update_status_page(
            db, status_page_id, StatusPageUpdate(**sent)
        )
        return _response(updated)

    @staticmethod
    async def replace_items(
        db: AsyncSession, status_page_id: UUID, data: AdminStatusPageItems
    ) -> AdminStatusPage:
        """The page's items become exactly these, in this order."""
        await StatusPageCoreService.get_status_page(db, status_page_id)  # 404 first
        await AdminStatusPageCoreService._require_references(db, data.items)
        updated = await StatusPageCoreService.update_status_page(
            db, status_page_id, StatusPageUpdate(items=_stored_items(data.items))
        )
        return _response(updated)

    @staticmethod
    async def delete_page(
        db: AsyncSession, status_page_id: UUID, owner_id: UUID
    ) -> MaintenanceJobResponse:
        """Queue the page's delete; the job re-judges its owner when it runs."""
        await StatusPageCoreService.get_status_page(db, status_page_id)  # 404 first
        return await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.STATUS_PAGE_DELETE,
            target_id=status_page_id,
            owner_id=owner_id,
        )
