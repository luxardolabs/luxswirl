"""
Base models and mixins for SQLAlchemy models.

Provides common patterns and utilities for all database models.
"""

import re
from datetime import datetime
from enum import Enum as PyEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, Integer, Uuid, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.sql import func

from app.core.datetime_utils import utc_now


def str_enum[EnumT: PyEnum](enum_cls: type[EnumT], length: int) -> Enum:
    """A VARCHAR column that stores a StrEnum's ``.value`` (not the member name)
    and reads it back as the enum.

    No native Postgres enum and no CHECK constraint — the column is a plain
    VARCHAR — so adding an enum member stays a one-line enum edit, never a
    database migration. ``values_callable`` is the non-obvious-but-required bit:
    without it SQLAlchemy persists the member *name* (``SECURITY``) instead of
    its value (``security``).
    """
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    # Common type annotation mapping
    type_annotation_map = {
        datetime: DateTime(timezone=True),
    }


class TimestampMixin:
    """Mixin for adding created_at and updated_at timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when record was created",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Timestamp when record was last updated",
    )


class SoftDeleteMixin:
    """Mixin for soft delete functionality."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="Timestamp when record was soft-deleted (NULL if active)",
    )

    @property
    def is_deleted(self) -> bool:
        """Check if record is soft-deleted."""
        return self.deleted_at is not None

    def soft_delete(self) -> None:
        """Mark record as deleted."""
        self.deleted_at = utc_now()

    def restore(self) -> None:
        """Restore soft-deleted record."""
        self.deleted_at = None


class TableNameMixin:
    """Mixin to automatically generate table name from class name."""

    @declared_attr.directive
    def __tablename__(cls) -> str:
        """Generate table name from class name (convert CamelCase to snake_case)."""
        # Get class name - cls is guaranteed to be a class type in this context
        name: str = cls.__name__  # type: ignore[attr-defined]  # cls is a class in @declared_attr context
        # Convert CamelCase to snake_case
        name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
        name = re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()
        # Pluralize (simple rule - add 's')
        if not name.endswith("s"):
            name += "s"
        return name


class PrimaryKeyMixin:
    """Mixin for auto-incrementing integer primary key."""

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="Auto-incrementing primary key",
    )


class UUIDPrimaryKeyMixin:
    """Mixin for UUID primary key."""

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid4,
        comment="UUID primary key",
    )


class SerializerMixin:
    """Mixin for model serialization."""

    def to_dict(
        self,
        exclude: set[str] | None = None,
        include_relationships: bool = False,
    ) -> dict[str, Any]:
        """
        Convert model instance to dictionary.

        Args:
            exclude: Set of field names to exclude
            include_relationships: Whether to include relationship data

        Returns:
            Dictionary representation of the model
        """
        exclude = exclude or set()
        result: dict[str, Any] = {}

        # Get all columns - ensure this is called on a SQLAlchemy model
        if not hasattr(self, "__table__"):
            return result

        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                # Convert datetime to ISO format
                if isinstance(value, datetime):
                    value = value.isoformat()
                result[column.name] = value

        # Optionally include relationships
        if include_relationships:
            mapper = inspect(self.__class__)
            if mapper is None:
                return result
            for relationship in mapper.relationships:
                if relationship.key not in exclude:
                    value = getattr(self, relationship.key)
                    if value is not None:
                        if hasattr(value, "__iter__") and not isinstance(value, str):
                            # Collection relationship
                            result[relationship.key] = [
                                (
                                    item.to_dict(exclude=exclude, include_relationships=False)
                                    if hasattr(item, "to_dict")
                                    else str(item)
                                )
                                for item in value
                            ]
                        else:
                            # Single relationship
                            result[relationship.key] = (
                                value.to_dict(exclude=exclude, include_relationships=False)
                                if hasattr(value, "to_dict")
                                else str(value)
                            )

        return result


class BaseModel(Base, PrimaryKeyMixin, TimestampMixin, TableNameMixin, SerializerMixin):
    """
    Base model with common functionality.

    Includes:
    - Auto-incrementing ID
    - created_at and updated_at timestamps
    - Automatic table naming (CamelCase -> snake_case)
    - Serialization methods
    """

    __abstract__ = True


class UUIDBaseModel(Base, UUIDPrimaryKeyMixin, TimestampMixin, TableNameMixin, SerializerMixin):
    """
    Base model with UUID primary key.

    Includes:
    - UUID ID
    - created_at and updated_at timestamps
    - Automatic table naming (CamelCase -> snake_case)
    - Serialization methods
    """

    __abstract__ = True

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        attrs = []
        for key in self.__mapper__.columns.keys():
            if key != "id":
                value = getattr(self, key, None)
                if value is not None:
                    # Truncate long strings
                    if isinstance(value, str) and len(value) > 50:
                        value = f"{value[:47]}..."
                    attrs.append(f"{key}={value!r}")
            if len(attrs) >= 3:  # Limit to first 3 attributes
                break

        attrs_str = ", ".join(attrs)
        return f"<{self.__class__.__name__}(id={self.id}, {attrs_str})>"
