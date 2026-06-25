"""
StatusPage service - business logic for status page operations.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes

from app.core.datetime_utils import utc_now
from app.core.exceptions import StatusPageNotFoundException
from app.crud.status_page_crud import StatusPageCRUD
from app.models.status_page_model import StatusPage
from app.schemas.status_page_schema import StatusPageCreate, StatusPageUpdate

logger = get_logger("luxswirl.services.status_page")


class StatusPageCoreService:
    """Service for status page operations."""

    @staticmethod
    async def get_status_page_by_id(db: AsyncSession, status_page_id: UUID) -> StatusPage:
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
    async def create_status_page(db: AsyncSession, data: StatusPageCreate) -> StatusPage:
        """
        Create a new status page.

        Args:
            db: Database session
            data: StatusPage creation data

        Returns:
            Created StatusPage object

        Raises:
            ValueError: If slug already exists
        """
        # Check if slug already exists
        existing = await StatusPageCoreService.get_status_page_by_slug(db, data.slug)
        if existing:
            raise ValueError(f"Status page with slug '{data.slug}' already exists")

        # Create new status page
        status_page = StatusPage(
            name=data.name,
            slug=data.slug,
            description=data.description,
            is_public=data.is_public,
            config=data.config,
            items=data.items,
        )

        db.add(status_page)
        await db.flush()
        await db.refresh(status_page)

        logger.info(
            "Created status page",
            extra={"slug": status_page.slug, "status_page_id": str(status_page.id)},
        )
        return status_page

    @staticmethod
    async def update_status_page(
        db: AsyncSession, status_page_id: UUID, data: StatusPageUpdate
    ) -> StatusPage:
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
            ValueError: If new slug already exists
        """
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)

        # Check slug uniqueness if updating slug
        if data.slug and data.slug != status_page.slug:
            existing = await StatusPageCoreService.get_status_page_by_slug(db, data.slug)
            if existing:
                raise ValueError(f"Status page with slug '{data.slug}' already exists")

        # Update fields
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(status_page, field, value)

        status_page.updated_at = utc_now()
        await db.flush()
        await db.refresh(status_page)

        logger.info(
            "Updated status page",
            extra={"slug": status_page.slug, "status_page_id": str(status_page.id)},
        )
        return status_page

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
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)

        await db.delete(status_page)
        await db.flush()

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
    ) -> tuple[Sequence[StatusPage], int]:
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
        return await StatusPageCRUD.list_paginated(
            db, is_public=is_public, offset=offset, limit=limit
        )

    @staticmethod
    async def add_check_to_page(
        db: AsyncSession, status_page_id: UUID, check_id: UUID, order: int | None = None
    ) -> StatusPage:
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
        """
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        status_page.add_check(check_id, order)
        status_page.updated_at = utc_now()

        await db.flush()
        await db.refresh(status_page)

        logger.info(
            "Added check to status page",
            extra={
                "check_id": str(check_id),
                "slug": status_page.slug,
                "status_page_id": str(status_page.id),
            },
        )
        return status_page

    @staticmethod
    async def add_group_to_page(
        db: AsyncSession,
        status_page_id: UUID,
        name: str,
        filter: dict[str, Any] | None = None,
        order: int | None = None,
        collapsed: bool = False,
    ) -> StatusPage:
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
        """
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        status_page.add_group(name, filter, order, collapsed)
        status_page.updated_at = utc_now()

        await db.flush()
        await db.refresh(status_page)

        logger.info(
            "Added group to status page",
            extra={
                "group_name": name,
                "slug": status_page.slug,
                "status_page_id": str(status_page.id),
            },
        )
        return status_page

    @staticmethod
    async def remove_item_from_page(
        db: AsyncSession, status_page_id: UUID, index: int
    ) -> StatusPage:
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
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        status_page.remove_item(index)
        status_page.updated_at = utc_now()

        await db.flush()
        await db.refresh(status_page)

        logger.info(
            "Removed item from status page",
            extra={
                "item_index": index,
                "slug": status_page.slug,
                "status_page_id": str(status_page.id),
            },
        )
        return status_page

    @staticmethod
    async def reorder_item(
        db: AsyncSession, status_page_id: UUID, from_index: int, to_index: int
    ) -> StatusPage:
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
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        status_page.reorder_item(from_index, to_index)
        status_page.updated_at = utc_now()

        await db.flush()
        await db.refresh(status_page)

        logger.info(
            "Reordered item in status page",
            extra={
                "slug": status_page.slug,
                "status_page_id": str(status_page.id),
                "from_index": from_index,
                "to_index": to_index,
            },
        )
        return status_page

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
        db: AsyncSession, status_page_id: UUID, client_items: list[dict]
    ) -> StatusPage:
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
        """

        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)

        logger.info(
            "Reorder request",
            extra={
                "old_items": status_page.items,
                "client_items": client_items,
            },
        )

        # Build new items array based on client state
        new_items = []
        for item_data in client_items:
            old_index = item_data.get("oldIndex")
            item_type = item_data.get("type")

            if item_type == "group":
                # Retrieve original group item
                if old_index is not None and 0 <= old_index < len(status_page.items):
                    group_item = status_page.items[old_index].copy()
                    # Update checks array if it's a container group
                    if "checks" in group_item:
                        group_item["checks"] = item_data.get("checks", [])
                    new_items.append(group_item)
            elif item_type == "check":
                # Retrieve original check item
                if old_index is not None and 0 <= old_index < len(status_page.items):
                    new_items.append(status_page.items[old_index])

        logger.info(
            "Reorder request - new items computed",
            extra={"new_items": new_items},
        )

        # Update the status page
        status_page.items = new_items
        status_page._reorder_items()
        status_page.updated_at = utc_now()
        attributes.flag_modified(status_page, "items")

        await db.flush()
        await db.refresh(status_page)

        logger.info(
            "Reordered all items in status page",
            extra={"slug": status_page.slug, "status_page_id": str(status_page.id)},
        )
        return status_page

    @staticmethod
    async def rename_group(
        db: AsyncSession, status_page_id: UUID, item_index: int, new_name: str
    ) -> StatusPage:
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
        """

        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)

        if 0 <= item_index < len(status_page.items):
            item = status_page.items[item_index]
            if item.get("type") == "group":
                item["name"] = new_name
                status_page.updated_at = utc_now()
                attributes.flag_modified(status_page, "items")

                await db.flush()
                await db.refresh(status_page)

                logger.info(
                    "Renamed group in status page",
                    extra={
                        "item_index": item_index,
                        "new_name": new_name,
                        "slug": status_page.slug,
                    },
                )

        return status_page

    @staticmethod
    async def update_group_filters(
        db: AsyncSession, status_page_id: UUID, item_index: int, filter: dict
    ) -> StatusPage:
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
        """

        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)

        if 0 <= item_index < len(status_page.items):
            item = status_page.items[item_index]
            if item.get("type") == "group" and "filter" in item:
                item["filter"] = filter
                status_page.updated_at = utc_now()
                attributes.flag_modified(status_page, "items")

                await db.flush()
                await db.refresh(status_page)

                logger.info(
                    "Updated filters for group in status page",
                    extra={
                        "item_index": item_index,
                        "slug": status_page.slug,
                    },
                )

        return status_page

    @staticmethod
    async def update_group_sort(
        db: AsyncSession,
        status_page_id: UUID,
        item_index: int,
        sort_by: str,
        sort_direction: str,
    ) -> StatusPage:
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
        """
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        status_page.update_group_sort(item_index, sort_by, sort_direction)
        status_page.updated_at = utc_now()

        await db.flush()
        await db.refresh(status_page)

        logger.info(
            "Updated sort for group in status page",
            extra={
                "item_index": item_index,
                "slug": status_page.slug,
                "sort_by": sort_by,
                "sort_direction": sort_direction,
            },
        )

        return status_page
