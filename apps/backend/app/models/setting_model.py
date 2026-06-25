"""
Setting model - stores configurable system defaults.
"""

from typing import Any

from sqlalchemy import JSON, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    SerializerMixin,
    TableNameMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    str_enum,
)
from app.models.enum_model import SettingCategory


class Setting(Base, UUIDPrimaryKeyMixin, TimestampMixin, TableNameMixin, SerializerMixin):
    """
    Setting model - stores configurable system defaults.

    Settings are organized by category and accessed by key.
    Example keys: "check.default_interval", "alert.consecutive_failures"
    """

    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("key", name="uq_settings_key"),
        Index("idx_settings_category", "category"),
        Index("idx_settings_key", "key"),
        Index("idx_settings_subcategory", "subcategory"),
    )

    # Setting identification
    key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        comment="Unique setting key (e.g., 'check.default_interval')",
    )

    category: Mapped[SettingCategory] = mapped_column(
        str_enum(SettingCategory, 50),
        nullable=False,
        comment="Setting category (check, alert, system, job, database, general, metrics, security)",
    )

    subcategory: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Optional subcategory for grouping settings within a category",
    )

    # Value storage (JSONB for flexibility)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        comment="Setting value (JSONB: {value: <actual>, type: <type>})",
    )
    # Example value:
    # {"value": 60, "type": "int"}
    # {"value": true, "type": "bool"}
    # {"value": "GET", "type": "string"}
    # {"value": [22, 80, 443], "type": "list"}

    # Metadata
    display_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable display name",
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Detailed description of what this setting controls",
    )

    default_value: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        comment="System default value (for reset functionality)",
    )

    # Validation metadata (optional)
    validation: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Validation rules (min, max, enum, etc.)",
    )
    # Example validation:
    # {"min": 1, "max": 3600}
    # {"enum": ["GET", "POST", "PUT"]}

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return (
            f"<Setting(id={self.id}, "
            f"key={self.key!r}, "
            f"category={self.category!r}, "
            f"value={self.value})>"
        )

    @property
    def typed_value(self) -> Any:
        """
        Get the actual typed value from the JSONB storage.

        Returns:
            The value in its proper Python type
        """
        return self.value.get("value")

    def set_typed_value(self, new_value: Any) -> None:
        """
        Set a new value while preserving type information.

        Args:
            new_value: The new value to store
        """
        # Infer type from value
        value_type = type(new_value).__name__
        if value_type == "int":
            value_type = "int"
        elif value_type == "float":
            value_type = "float"
        elif value_type == "bool":
            value_type = "bool"
        elif value_type == "str":
            value_type = "string"
        elif value_type == "list":
            value_type = "list"
        elif value_type == "dict":
            value_type = "dict"
        else:
            value_type = "string"

        self.value = {"value": new_value, "type": value_type}
