"""
StatusPage model - represents custom status pages/dashboards.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Boolean, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, attributes, mapped_column

from app.models.base import UUIDBaseModel

if TYPE_CHECKING:
    pass


class StatusPage(UUIDBaseModel):
    """
    StatusPage model - stores custom status page/dashboard definitions.

    Each status page has a unique slug for public access and contains
    a JSONB array of items (checks and groups) that define what to display.
    """

    __tablename__ = "status_pages"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_status_page_slug"),
        Index("idx_status_pages_slug", "slug"),
        Index("idx_status_pages_is_public", "is_public"),
    )

    # Basic info
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Display name of the status page",
    )

    slug: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        comment="URL-friendly slug for public access (e.g., /status/production)",
    )

    description: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
        comment="Optional description of the status page",
    )

    # Visibility
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Whether the status page is publicly accessible",
    )

    # Configuration (theme, display options, etc.)
    config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Display configuration (theme, show_uptime, etc.)",
    )

    # Items array - ordered list of checks and groups
    items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="Ordered array of items to display (checks and groups)",
    )

    # Helper methods
    def add_check(self, check_id: UUID, order: int | None = None) -> None:
        """Add a check to the status page."""
        if order is None:
            order = len(self.items)

        item = {
            "type": "check",
            "check_id": str(check_id),  # Store UUID as string in JSONB
            "order": order,
        }
        self.items.append(item)
        self._reorder_items()
        # Mark the column as modified for SQLAlchemy to track the change
        attributes.flag_modified(self, "items")

    def add_group(
        self,
        name: str,
        filter: dict[str, Any] | None = None,
        order: int | None = None,
        collapsed: bool = False,
        sort_by: str = "manual",
        sort_direction: str = "asc",
    ) -> None:
        """Add a group to the status page.

        Groups can be either:
        - Container groups: Hold specific checks (filter is None, has 'checks' array)
        - Filter-based groups: Dynamically show checks matching filters (has 'filter' dict)

        Args:
            name: Group display name
            filter: Dynamic filter config (None for container groups)
            order: Display order
            collapsed: Whether group starts collapsed
            sort_by: Sort criteria (manual, name, status, latency, uptime)
            sort_direction: Sort direction (asc, desc)
        """
        if order is None:
            order = len(self.items)

        item = {
            "type": "group",
            "name": name,
            "order": order,
            "collapsed": collapsed,
            "sort_by": sort_by,
            "sort_direction": sort_direction,
        }

        # Container group (static - holds specific checks)
        if filter is None:
            item["checks"] = []
        # Filter-based group (dynamic - filters checks)
        else:
            item["filter"] = filter  # {"agent_id": "x", "tags": ["prod"]}

        self.items.append(item)
        self._reorder_items()
        # Mark the column as modified for SQLAlchemy to track the change
        attributes.flag_modified(self, "items")

    def remove_item(self, index: int) -> None:
        """Remove an item by index."""
        if 0 <= index < len(self.items):
            self.items.pop(index)
            self._reorder_items()
            # Mark the column as modified for SQLAlchemy to track the change
            from sqlalchemy.orm import attributes

            attributes.flag_modified(self, "items")

    def reorder_item(self, from_index: int, to_index: int) -> None:
        """Move an item from one position to another."""
        if 0 <= from_index < len(self.items) and 0 <= to_index < len(self.items):
            item = self.items.pop(from_index)
            self.items.insert(to_index, item)
            self._reorder_items()
            # Mark the column as modified for SQLAlchemy to track the change
            from sqlalchemy.orm import attributes

            attributes.flag_modified(self, "items")

    def update_group_sort(self, index: int, sort_by: str, sort_direction: str) -> None:
        """Update sort settings for a group.

        Args:
            index: Group index in items array
            sort_by: Sort criteria (manual, name, status, latency, uptime)
            sort_direction: Sort direction (asc, desc)
        """
        if 0 <= index < len(self.items):
            item = self.items[index]
            if item.get("type") == "group":
                item["sort_by"] = sort_by
                item["sort_direction"] = sort_direction
                # Mark the column as modified for SQLAlchemy to track the change
                from sqlalchemy.orm import attributes

                attributes.flag_modified(self, "items")

    def _reorder_items(self) -> None:
        """Ensure items are properly ordered."""
        for i, item in enumerate(self.items):
            item["order"] = i

    @property
    def check_count(self) -> int:
        """Count of direct check items (not including group checks)."""
        return sum(1 for item in self.items if item.get("type") == "check")

    @property
    def group_count(self) -> int:
        """Count of group items."""
        return sum(1 for item in self.items if item.get("type") == "group")

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return (
            f"<StatusPage(id={self.id}, name={self.name!r}, "
            f"slug={self.slug!r}, items={len(self.items)})>"
        )
