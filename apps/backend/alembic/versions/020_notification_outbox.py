"""notification_outbox — deliver alert notifications from the notification log.

Alert evaluation writes each notification it decides as a `pending` row of
`notification_logs`, with the notification itself in `payload`, in the
transaction that decides it; a delivery worker claims due rows, delivers them
outside any transaction and records the outcome. The columns below hold that
queue: the order rows were decided in, the payload, the attempts made, when a
row is next due, when a worker's claim lapses, and when delivery ended.

Every row already stored is a finished delivery: the new columns stay empty
for it, and `decision_seq` numbers those rows in whatever order the table
holds them, which no delivery reads.

Revision ID: 7c3e9a2d4b61
Revises: 5d2b8e4f7a19
Create Date: 2026-11-15

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "7c3e9a2d4b61"
down_revision = "5d2b8e4f7a19"
branch_labels = None
depends_on = None


_SEQUENCE = "notification_decision_order"


_STATUS_COMMENT = (
    "Status of notification (pending, sending, retrying, sent, failed, "
    "rate_limited, deduplicated, suppressed)"
)
_OLD_STATUS_COMMENT = (
    "Status of notification (sent, failed, retrying, rate_limited, deduplicated, "
    "suppressed)"
)

_SENT_AT_COMMENT = (
    "When the notification was decided (delivered_at is when its delivery ended)"
)
_OLD_SENT_AT_COMMENT = "When notification was sent"


def _comment(column: str, type_: sa.String | sa.DateTime, new: str, old: str) -> None:
    op.alter_column(
        "notification_logs",
        column,
        existing_type=type_,
        existing_nullable=False,
        comment=new,
        existing_comment=old,
    )


def upgrade() -> None:
    _comment("status", sa.String(length=50), _STATUS_COMMENT, _OLD_STATUS_COMMENT)
    _comment(
        "sent_at", sa.DateTime(timezone=True), _SENT_AT_COMMENT, _OLD_SENT_AT_COMMENT
    )
    op.execute(sa.schema.CreateSequence(sa.Sequence(_SEQUENCE)))
    # The default numbers every row already stored as the column is added.
    op.add_column(
        "notification_logs",
        sa.Column(
            "decision_seq",
            sa.BigInteger(),
            server_default=sa.text(f"nextval('{_SEQUENCE}'::regclass)"),
            nullable=False,
            comment=(
                "Order the rows were decided in: one alert's notifications about "
                "one subject are delivered to a provider in this order"
            ),
        ),
    )
    op.add_column(
        "notification_logs",
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=True,
            comment=(
                "The notification to deliver (NotificationContext) while it awaits "
                "delivery; cleared once delivery ends"
            ),
        ),
    )
    op.add_column(
        "notification_logs",
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment="Delivery attempts made",
        ),
    )
    op.add_column(
        "notification_logs",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When a pending or retrying row is next due for delivery",
        ),
    )
    op.add_column(
        "notification_logs",
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When a sending row's claim lapses and another worker may deliver it",
        ),
    )
    op.add_column(
        "notification_logs",
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When delivery ended (sent, failed, suppressed or rate limited by "
                "the worker)"
            ),
        ),
    )
    op.execute(f"ALTER SEQUENCE {_SEQUENCE} OWNED BY notification_logs.decision_seq")
    op.create_index(
        "idx_notification_logs_undelivered",
        "notification_logs",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'sending', 'retrying')"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_notification_logs_undelivered",
        table_name="notification_logs",
        postgresql_where=sa.text("status IN ('pending', 'sending', 'retrying')"),
    )
    op.drop_column("notification_logs", "delivered_at")
    op.drop_column("notification_logs", "lease_expires_at")
    op.drop_column("notification_logs", "next_attempt_at")
    op.drop_column("notification_logs", "attempts")
    op.drop_column("notification_logs", "payload")
    op.drop_column("notification_logs", "decision_seq")
    op.execute(sa.schema.DropSequence(sa.Sequence(_SEQUENCE), if_exists=True))
    _comment(
        "sent_at", sa.DateTime(timezone=True), _OLD_SENT_AT_COMMENT, _SENT_AT_COMMENT
    )
    _comment("status", sa.String(length=50), _OLD_STATUS_COMMENT, _STATUS_COMMENT)
