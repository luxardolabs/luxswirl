"""agent_dead_letter_count — how many batches the server refused for good.

The agent dead-letters a report batch the server refuses permanently (400, 413,
422) instead of retrying it forever, and reports how many it holds in each
heartbeat. The count is stored on the agent row so the agents page can warn.

NULL until an agent that reports it sends a heartbeat.

Revision ID: 31d253521b47
Revises: 2c806c09128d
Create Date: 2026-10-22

"""

import sqlalchemy as sa
from alembic import op

revision = "31d253521b47"
down_revision = "2c806c09128d"
branch_labels = None
depends_on = None

_COMMENT = "Report batches the agent dead-lettered after a permanent refusal"


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "dead_letter_count",
            sa.Integer(),
            nullable=True,
            comment=_COMMENT,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "dead_letter_count")
