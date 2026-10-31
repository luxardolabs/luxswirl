"""
StatusPage service - business logic for status page operations.
"""

from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes

from app.core.datetime_utils import utc_now
from app.core.exceptions import (
    DuplicateResourceException,
    StatusPageNotFoundException,
)
from app.crud.status_page_crud import StatusPageCRUD
from app.models.status_page_model import StatusPage
from app.schemas.admin_status_page_schema import (
    MAX_ITEMS,
    MAX_PAGE_CHECKS,
    EditorReorder,
    GroupFilter,
    GroupName,
    SortBy,
    SortDirection,
)
from app.schemas.status_page_schema import (
    StatusPageCreate,
    StatusPageInDB,
    StatusPageUpdate,
)
from app.services.core.check_core_service import CheckCoreService
from app.shared.logger import get_logger

_GROUP_NAME = TypeAdapter(GroupName)
_REORDER = TypeAdapter(EditorReorder)
_SORT_BY = TypeAdapter(SortBy)
_SORT_DIRECTION = TypeAdapter(SortDirection)


def _named_check_ids(items: list[dict[str, Any]]) -> set[str]:
    """The check ids a page's items name: its check items and its groups' lists."""
    named = {i["check_id"] for i in items if i.get("type") == "check"}
    for item in items:
        named.update(item.get("checks") or [])
    return named


def _room_for_one_more(items: list[dict[str, Any]]) -> None:
    """Refuse an item past what one page holds, the most a reorder takes."""
    if len(items) >= MAX_ITEMS:
        raise ValueError(f"A page holds at most {MAX_ITEMS} items")


def _stored_filter(filter: dict[str, Any]) -> dict[str, Any]:
    """A group filter in the form the admin API stores it, or ValueError when it
    is not one."""
    return GroupFilter.model_validate(filter).model_dump(mode="json", exclude_none=True)


logger = get_logger("luxswirl.services.status_page")


def _slug_taken(slug: str) -> DuplicateResourceException:
    return DuplicateResourceException(f"Status page with slug '{slug}' already exists")


class StatusPageCoreService:
    """Service for status page operations."""

    @staticmethod
    async def get_status_page_by_id(
        db: AsyncSession, status_page_id: UUID
    ) -> StatusPage:
        """
        Get status page by UUID.

        Args:
            db: Database session
            status_page_id: Status page UUID

        Returns:
            StatusPage object

        Raises:
            StatusPageNotFoundException: If status page not found
        """
        status_page = await StatusPageCRUD.get_by_id(db, status_page_id)
        if not status_page:
            raise StatusPageNotFoundException(str(status_page_id))
        return status_page

    @staticmethod
    async def get_status_page(db: AsyncSession, status_page_id: UUID) -> StatusPageInDB:
        """The status page as the web seam holds it — a DTO, not the ORM row.

        get_status_page_by_id stays ORM: eleven methods in this service read it
        and then mutate the row's items.
        """
        return StatusPageInDB.model_validate(
            await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        )

    @staticmethod
    async def get_page_by_slug(db: AsyncSession, slug: str) -> StatusPageInDB | None:
        """The slug lookup, as a DTO for the seam."""
        page = await StatusPageCoreService.get_status_page_by_slug(db, slug)
        return StatusPageInDB.model_validate(page) if page else None

    @staticmethod
    async def get_status_page_by_slug(db: AsyncSession, slug: str) -> StatusPage | None:
        """
        Get status page by slug.

        Args:
            db: Database session
            slug: URL-friendly slug

        Returns:
            StatusPage object or None if not found
        """
        return await StatusPageCRUD.get_by_slug(db, slug)

    @staticmethod
    async def create_status_page(
        db: AsyncSession, data: StatusPageCreate
    ) -> StatusPageInDB:
        """
        Create a new status page.

        Args:
            db: Database session
            data: StatusPage creation data

        Returns:
            Created StatusPage object

        Raises:
            DuplicateResourceException: If slug already exists
        """
        # Check if slug already exists
        existing = await StatusPageCoreService.get_status_page_by_slug(db, data.slug)
        if existing:
            raise _slug_taken(data.slug)

        # Create new status page
        status_page = StatusPage(
            name=data.name,
            slug=data.slug,
            description=data.description,
            is_public=data.is_public,
            config=data.config,
            items=data.items,
        )

        try:
            status_page = await StatusPageCRUD.add_page(db, status_page)
        except IntegrityError as e:
            # Another request took the slug between the check and the insert.
            raise _slug_taken(data.slug) from e

        logger.info(
            "Created status page",
            extra={"slug": status_page.slug, "status_page_id": str(status_page.id)},
        )
        return StatusPageInDB.model_validate(status_page)

    @staticmethod
    async def update_status_page(
        db: AsyncSession, status_page_id: UUID, data: StatusPageUpdate
    ) -> StatusPageInDB:
        """
        Update a status page.

        Args:
            db: Database session
            status_page_id: Status page UUID
            data: Update data

        Returns:
            Updated StatusPage object

        Raises:
            StatusPageNotFoundException: If status page not found
            DuplicateResourceException: If new slug already exists
        """
        status_page = await StatusPageCoreService.get_status_page_by_id(
            db, status_page_id
        )

        # Check slug uniqueness if updating slug
        if data.slug and data.slug != status_page.slug:
            existing = await StatusPageCoreService.get_status_page_by_slug(
                db, data.slug
            )
            if existing:
                raise _slug_taken(data.slug)

        changes = {**data.model_dump(exclude_unset=True), "updated_at": utc_now()}
        try:
            status_page = await StatusPageCRUD.update_page(db, status_page, changes)
        except IntegrityError as e:
            # Another request took the slug between the check and the write.
            raise _slug_taken(data.slug or "") from e

        logger.info(
            "Updated status page",
            extra={"slug": status_page.slug, "status_page_id": str(status_page.id)},
        )
        return StatusPageInDB.model_validate(status_page)

    @staticmethod
    async def delete_status_page(db: AsyncSession, status_page_id: UUID) -> None:
        """
        Delete a status page.

        Args:
            db: Database session
            status_page_id: Status page UUID

        Raises:
            StatusPageNotFoundException: If status page not found
        """
        status_page = await StatusPageCoreService.get_status_page_by_id(
            db, status_page_id
        )

        await StatusPageCRUD.delete_page(db, status_page)

        logger.info(
            "Deleted status page",
            extra={"slug": status_page.slug, "status_page_id": str(status_page.id)},
        )

    @staticmethod
    async def list_status_pages(
        db: AsyncSession,
        is_public: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[StatusPageInDB], int]:
        """
        List status pages with pagination and filtering.

        Args:
            db: Database session
            is_public: Filter by public/private status (None = all)
            limit: Max results
            offset: Pagination offset

        Returns:
            Tuple of (status pages list, total count)
        """
        pages, total = await StatusPageCRUD.list_paginated(
            db, is_public=is_public, skip=offset, limit=limit
        )
        return [StatusPageInDB.model_validate(p) for p in pages], total

    @staticmethod
    async def add_check_to_page(
        db: AsyncSession, status_page_id: UUID, check_id: UUID, order: int | None = None
    ) -> StatusPageInDB:
        """
        Add a check to a status page.

        Args:
            db: Database session
            status_page_id: Status page UUID
            check_id: Check UUID
            order: Display order (None = append to end)

        Returns:
            Updated StatusPage object

        Raises:
            StatusPageNotFoundException: If status page not found
            ValueError: If the page is full or no check has the id
        """
        status_page = await StatusPageCoreService.get_status_page_by_id(
            db, status_page_id
        )
        _room_for_one_more(status_page.items)
        if len(_named_check_ids(status_page.items) | {str(check_id)}) > MAX_PAGE_CHECKS:
            raise ValueError(f"A page names at most {MAX_PAGE_CHECKS} checks")
        # The check must exist, as the admin API and the reorder require.
        if not await CheckCoreService.get_checks_by_ids(db, [check_id]):
            raise ValueError(f"No check has the id {check_id}")
        status_page.add_check(check_id, order)
        status_page.updated_at = utc_now()

        status_page = await StatusPageCRUD.save_page(db, status_page)

        logger.info(
            "Added check to status page",
            extra={
                "check_id": str(check_id),
                "slug": status_page.slug,
                "status_page_id": str(status_page.id),
            },
        )
        return StatusPageInDB.model_validate(status_page)

    @staticmethod
    async def add_group_to_page(
        db: AsyncSession,
        status_page_id: UUID,
        name: str,
        filter: dict[str, Any] | None = None,
        order: int | None = None,
        collapsed: bool = False,
    ) -> StatusPageInDB:
        """
        Add a group to a status page.

        Args:
            db: Database session
            status_page_id: Status page UUID
            name: Group display name
            filter: Filter configuration for checks in group
            order: Display order (None = append to end)
            collapsed: Whether group is collapsed by default

        Returns:
            Updated StatusPage object

        Raises:
            StatusPageNotFoundException: If status page not found
            ValueError: If the name or filter is not one a group takes
        """
        name = _GROUP_NAME.validate_python(name)
        if filter is not None:
            filter = _stored_filter(filter)
        status_page = await StatusPageCoreService.get_status_page_by_id(
            db, status_page_id
        )
        _room_for_one_more(status_page.items)
        status_page.add_group(name, filter, order, collapsed)
        status_page.updated_at = utc_now()

        status_page = await StatusPageCRUD.save_page(db, status_page)

        logger.info(
            "Added group to status page",
            extra={
                "group_name": name,
                "slug": status_page.slug,
                "status_page_id": str(status_page.id),
            },
        )
        return StatusPageInDB.model_validate(status_page)

    @staticmethod
    async def remove_item_from_page(
        db: AsyncSession, status_page_id: UUID, index: int
    ) -> StatusPageInDB:
        """
        Remove an item from a status page by index.

        Args:
            db: Database session
            status_page_id: Status page UUID
            index: Item index to remove

        Returns:
            Updated StatusPage object

        Raises:
            StatusPageNotFoundException: If status page not found
        """
        status_page = await StatusPageCoreService.get_status_page_by_id(
            db, status_page_id
        )
        status_page.remove_item(index)
        status_page.updated_at = utc_now()

        status_page = await StatusPageCRUD.save_page(db, status_page)

        logger.info(
            "Removed item from status page",
            extra={
                "item_index": index,
                "slug": status_page.slug,
                "status_page_id": str(status_page.id),
            },
        )
        return StatusPageInDB.model_validate(status_page)

    @staticmethod
    async def reorder_item(
        db: AsyncSession, status_page_id: UUID, from_index: int, to_index: int
    ) -> StatusPageInDB:
        """
        Reorder an item in a status page.

        Args:
            db: Database session
            status_page_id: Status page UUID
            from_index: Current position of item
            to_index: New position for item

        Returns:
            Updated StatusPage object

        Raises:
            StatusPageNotFoundException: If status page not found
        """
        status_page = await StatusPageCoreService.get_status_page_by_id(
            db, status_page_id
        )
        status_page.reorder_item(from_index, to_index)
        status_page.updated_at = utc_now()

        status_page = await StatusPageCRUD.save_page(db, status_page)

        logger.info(
            "Reordered item in status page",
            extra={
                "slug": status_page.slug,
                "status_page_id": str(status_page.id),
                "from_index": from_index,
                "to_index": to_index,
            },
        )
        return StatusPageInDB.model_validate(status_page)

    @staticmethod
    async def get_public_count(db: AsyncSession) -> int:
        """
        Get count of public status pages.

        Args:
            db: Database session

        Returns:
            Count of public status pages
        """
        return await StatusPageCRUD.count_public(db)

    @staticmethod
    async def get_private_count(db: AsyncSession) -> int:
        """
        Get count of private status pages.

        Args:
            db: Database session

        Returns:
            Count of private status pages
        """
        return await StatusPageCRUD.count_private(db)

    @staticmethod
    async def reorder_all_items(
        db: AsyncSession, status_page_id: UUID, client_items: list[dict[str, Any]]
    ) -> StatusPageInDB:
        """
        Reorder all items in a status page based on client state.

        Processes client item data and rebuilds the items array maintaining
        original item data while applying new order and group membership.

        Args:
            db: Database session
            status_page_id: Status page UUID
            client_items: Items array from client (with oldIndex, type, checks)

        Returns:
            Updated StatusPage object

        Raises:
            StatusPageNotFoundException: If status page not found
            ValueError: If an item is not one the editor sends
        """
        reordered = _REORDER.validate_python(client_items)

        status_page = await StatusPageCoreService.get_status_page_by_id(
            db, status_page_id
        )

        logger.info(
            "Reorder request",
            extra={
                "old_items": status_page.items,
                "client_items": client_items,
            },
        )

        # Build new items array based on client state
        new_items = []
        placed_indexes = [
            item.old_index for item in reordered if item.old_index is not None
        ]
        placed = set(placed_indexes)
        if len(placed_indexes) != len(placed):
            raise ValueError("An item appears in the reorder more than once")
        for item in reordered:
            if item.old_index is None:
                # A check dragged out of a group: a check item of its own.
                new_items.append(
                    {"type": "check", "check_id": str(UUID(str(item.check_id)))}
                )
                continue
            if item.old_index >= len(status_page.items):
                continue
            if item.type == "group":
                group_item = status_page.items[item.old_index].copy()
                # A group that lists its checks takes the ones the editor sent.
                if "checks" in group_item:
                    group_item["checks"] = [
                        str(c) for c in dict.fromkeys(item.checks or [])
                    ]
                new_items.append(group_item)
            else:
                new_items.append(status_page.items[item.old_index])

        logger.info(
            "Reorder request - new items computed",
            extra={"new_items": new_items},
        )

        named = _named_check_ids(new_items)
        if len(named) > MAX_PAGE_CHECKS:
            raise ValueError(f"A page names at most {MAX_PAGE_CHECKS} checks")
        # Only a check dragged into a group leaves its own place; any other item
        # left out of the reorder would be deleted with it.
        grouped = {c for i in new_items for c in i.get("checks") or []}
        for index, stored in enumerate(status_page.items):
            if index not in placed and not (
                stored.get("type") == "check" and stored.get("check_id") in grouped
            ):
                raise ValueError("The reorder leaves out an item of the page")
        # A check new to the page must exist, as the admin API requires; a check
        # the page names may leave it only once it no longer exists, since a
        # reorder moves checks and removing one is the remove action's.
        stored_ids = _named_check_ids(status_page.items)
        new_ids = named - stored_ids
        lost_ids = stored_ids - named
        found = {
            str(c.id)
            for c in await CheckCoreService.get_checks_by_ids(
                db, [UUID(c) for c in new_ids | lost_ids]
            )
        }
        missing = sorted(new_ids - found)
        if missing:
            raise ValueError(f"No check has the id {missing[0]}")
        if lost_ids & found:
            raise ValueError("The reorder leaves out a check of the page")

        # Update the status page
        status_page.items = new_items
        status_page._reorder_items()
        status_page.updated_at = utc_now()
        attributes.flag_modified(status_page, "items")

        status_page = await StatusPageCRUD.save_page(db, status_page)

        logger.info(
            "Reordered all items in status page",
            extra={"slug": status_page.slug, "status_page_id": str(status_page.id)},
        )
        return StatusPageInDB.model_validate(status_page)

    @staticmethod
    async def rename_group(
        db: AsyncSession, status_page_id: UUID, item_index: int, new_name: str
    ) -> StatusPageInDB:
        """
        Rename a group in a status page.

        Args:
            db: Database session
            status_page_id: Status page UUID
            item_index: Index of group item
            new_name: New group name

        Returns:
            Updated StatusPage object

        Raises:
            StatusPageNotFoundException: If status page not found
            ValueError: If the name is not one a group takes
        """
        new_name = _GROUP_NAME.validate_python(new_name)
        status_page = await StatusPageCoreService.get_status_page_by_id(
            db, status_page_id
        )

        if 0 <= item_index < len(status_page.items):
            item = status_page.items[item_index]
            if item.get("type") == "group":
                item["name"] = new_name
                status_page.updated_at = utc_now()
                attributes.flag_modified(status_page, "items")

                status_page = await StatusPageCRUD.save_page(db, status_page)

                logger.info(
                    "Renamed group in status page",
                    extra={
                        "item_index": item_index,
                        "new_name": new_name,
                        "slug": status_page.slug,
                    },
                )

        return StatusPageInDB.model_validate(status_page)

    @staticmethod
    async def update_group_filters(
        db: AsyncSession, status_page_id: UUID, item_index: int, filter: dict[str, Any]
    ) -> StatusPageInDB:
        """
        Update filters for a dynamic filter group.

        Args:
            db: Database session
            status_page_id: Status page UUID
            item_index: Index of group item
            filter: New filter configuration

        Returns:
            Updated StatusPage object

        Raises:
            StatusPageNotFoundException: If status page not found
            ValueError: If the filter is not one a group takes
        """
        filter = _stored_filter(filter)

        status_page = await StatusPageCoreService.get_status_page_by_id(
            db, status_page_id
        )

        if 0 <= item_index < len(status_page.items):
            item = status_page.items[item_index]
            if item.get("type") == "group" and "filter" in item:
                item["filter"] = filter
                status_page.updated_at = utc_now()
                attributes.flag_modified(status_page, "items")

                status_page = await StatusPageCRUD.save_page(db, status_page)

                logger.info(
                    "Updated filters for group in status page",
                    extra={
                        "item_index": item_index,
                        "slug": status_page.slug,
                    },
                )

        return StatusPageInDB.model_validate(status_page)

    @staticmethod
    async def update_group_sort(
        db: AsyncSession,
        status_page_id: UUID,
        item_index: int,
        sort_by: str,
        sort_direction: str,
    ) -> StatusPageInDB:
        """
        Update sort settings for a group.

        Args:
            db: Database session
            status_page_id: Status page UUID
            item_index: Index of group item
            sort_by: Sort field
            sort_direction: Sort direction (asc/desc)

        Returns:
            Updated StatusPage object

        Raises:
            StatusPageNotFoundException: If status page not found
            ValueError: If the sort is not one a group takes
        """
        sort_by = _SORT_BY.validate_python(sort_by)
        sort_direction = _SORT_DIRECTION.validate_python(sort_direction)
        status_page = await StatusPageCoreService.get_status_page_by_id(
            db, status_page_id
        )
        status_page.update_group_sort(item_index, sort_by, sort_direction)
        status_page.updated_at = utc_now()

        status_page = await StatusPageCRUD.save_page(db, status_page)

        logger.info(
            "Updated sort for group in status page",
            extra={
                "item_index": item_index,
                "slug": status_page.slug,
                "sort_by": sort_by,
                "sort_direction": sort_direction,
            },
        )

        return StatusPageInDB.model_validate(status_page)
