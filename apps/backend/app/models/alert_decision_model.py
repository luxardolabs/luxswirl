"""
Alert decision queue and cursors - the results awaiting their alert decisions.

A report stores its results and, in the same transaction, queues the key of
each one an alert evaluates (`AlertDecisionQueue`); it decides nothing. The
alert decision worker (`app/background/alert_decision_worker.py`) takes each
check's queued results oldest first, decides their alerts, writes each
notification as a `pending` row of `notification_logs` and moves the check's
cursor (`AlertDecisionCursor`) to the newest result it decided, all in one
short transaction per check. A queued result no newer than its check's cursor
is stale (a newer one was decided first) and is dropped undecided.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base_model import Base


class AlertDecisionQueue(Base):
    """A stored result whose alerts are still to be decided."""

    __tablename__ = "alert_decision_queue"

    check_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("checks.id", ondelete="CASCADE"),
        primary_key=True,
        comment="The result's check",
    )

    result_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        comment="The result's timestamp: with check_id, its key in check_results",
    )


class AlertDecisionCursor(Base):
    """How far a check's results have had their alerts decided."""

    __tablename__ = "alert_decision_cursors"

    check_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("checks.id", ondelete="CASCADE"),
        primary_key=True,
        comment="The check",
    )

    decided_through: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment=(
            "Timestamp of the newest result whose alerts are decided: a result "
            "no newer is stale and never decided"
        ),
    )
