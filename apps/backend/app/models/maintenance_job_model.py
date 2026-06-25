"""Maintenance job model — backend-internal intent rows for cascading mutations.

Distinct from `models/job_model.Job`, which is user-facing agent dispatch. These
rows are never shown on the Jobs page; they exist solely so a long mutation
(agent delete cascade, bulk delete, bulk import) can be committed as intent
inside the web request and then executed by the in-process worker on its own
DB session.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Index, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.datetime_utils import utc_now
from app.models.base import Base, str_enum
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus


class MaintenanceJob(Base):
    __tablename__ = "maintenance_jobs"
    __table_args__ = (
        Index("idx_maint_jobs_status_created", "status", "created_at"),
        # Idempotency: at most one queued-or-running row per (kind, target_id).
        # Web routes catch the unique violation and return the existing job's
        # status partial so double-clicks don't double-enqueue.
        Index(
            "uq_maint_jobs_inflight",
            "kind",
            "target_id",
            unique=True,
            postgresql_where="status IN ('queued', 'running')",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)

    kind: Mapped[MaintenanceJobKind] = mapped_column(
        str_enum(MaintenanceJobKind, 50),
        nullable=False,
        comment="MaintenanceJobKind value (agent_delete, bulk_check_delete, etc.)",
    )

    target_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        comment="Primary entity being mutated (agent_id, status_page_id, etc.)",
    )

    params: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Kind-specific args (check_ids list, URL list, etc.)",
    )

    status: Mapped[MaintenanceJobStatus] = mapped_column(
        str_enum(MaintenanceJobStatus, 20),
        nullable=False,
        default=MaintenanceJobStatus.QUEUED,
        comment="MaintenanceJobStatus value (queued, running, done, failed)",
    )

    owner_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        comment="User who initiated this job (audit trail)",
    )

    progress: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Optional partial-progress payload (processed/total/message)",
    )

    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Failure reason when status='failed'",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<MaintenanceJob(id={self.id}, kind={self.kind!r}, "
            f"status={self.status!r}, target_id={self.target_id})>"
        )
