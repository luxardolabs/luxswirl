"""
CheckArtifact model - stores screenshots, traces, and other binary artifacts from checks.
"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base, SerializerMixin, TableNameMixin, str_enum
from app.models.enum_model import CheckArtifactType

if TYPE_CHECKING:
    from app.models.check_model import Check


class CheckArtifact(Base, TableNameMixin, SerializerMixin):
    """
    CheckArtifact model - stores binary artifacts from check executions.

    Used primarily for synthetic checks to store:
    - Screenshots (PNG)
    - Playwright traces (ZIP)
    - Videos (MP4) - future
    - HAR files (JSON) - future

    This table is a TimescaleDB hypertable partitioned by created_at.
    The composite primary key (id, created_at) is required for hypertable partitioning.
    """

    __table_args__ = (
        # Note: FK to check_results removed to allow TimescaleDB compression.
        # Both tables are hypertables with retention policies, so data ages out together.
        Index("idx_check_artifacts_result_id", "check_result_id", "check_result_timestamp"),
        Index("idx_check_artifacts_check_id", "check_id"),
        Index("idx_check_artifacts_type", "artifact_type"),
        Index("idx_check_artifacts_created", "created_at"),
    )

    # Composite primary key for TimescaleDB hypertable
    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid4,
        comment="UUID primary key (part of composite PK with created_at)",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when artifact was created (part of composite PK for TimescaleDB)",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Timestamp when record was last updated",
    )

    # Foreign keys
    check_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("checks.id", ondelete="CASCADE"),
        nullable=False,
        comment="Check that generated this artifact",
    )

    # Reference to check_results (no FK - both tables are hypertables with retention)
    check_result_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        comment="Check result ID (reference only, no FK for TimescaleDB compression)",
    )

    check_result_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Check result timestamp (reference only, no FK for TimescaleDB compression)",
    )

    # Artifact metadata
    artifact_type: Mapped[CheckArtifactType] = mapped_column(
        str_enum(CheckArtifactType, 20),
        nullable=False,
        comment="Type: screenshot, trace, video, har",
    )

    content_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="MIME type: image/png, application/zip, etc",
    )

    filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Original filename for download",
    )

    size_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Size of binary data in bytes",
    )

    # Binary data (PostgreSQL BYTEA, max ~1GB)
    data: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment="Binary artifact data",
    )

    # Relationships (created_at and updated_at provided by UUIDBaseModel mixin)
    check: Mapped[Check] = relationship(
        "Check",
        back_populates="artifacts",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<CheckArtifact(id={self.id}, check_id={self.check_id}, "
            f"type={self.artifact_type}, size={self.size_bytes})>"
        )
