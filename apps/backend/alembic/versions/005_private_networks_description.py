"""private_networks_description — the private-network setting names every range it blocks.

Block Private Network Targets also refuses loopback, the unspecified address and
CGNAT. Settings are seeded once and never overwritten, so a database seeded
earlier keeps the old description; this rewrites it, and only while it is still
the seeded text.

Revision ID: 5d1f0c2a9e47
Revises: b3b0189fbbe3
Create Date: 2026-10-24

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "5d1f0c2a9e47"
down_revision = "b3b0189fbbe3"
branch_labels = None
depends_on = None

KEY = "security.block_private_networks"
OLD = "Block checks from targeting RFC 1918 private networks (10.x, 172.16-31.x, 192.168.x). Disabled by default since most self-hosted users monitor internal services."
NEW = "Block checks from targeting private and internal addresses: RFC 1918 (10.x, 172.16-31.x, 192.168.x), IPv6 unique-local, loopback (127.x, ::1), 0.0.0.0 and CGNAT (100.64.x). Disabled by default since most self-hosted users monitor internal services."


def rewrite(connection: Connection, from_: str, to: str) -> None:
    """Replace the setting's description `from_` with `to`; any other text stays."""
    connection.execute(
        sa.text(
            "UPDATE settings SET description = :to "
            "WHERE key = :key AND description = :from_"
        ),
        {"to": to, "key": KEY, "from_": from_},
    )


def upgrade() -> None:
    rewrite(op.get_bind(), OLD, NEW)


def downgrade() -> None:
    rewrite(op.get_bind(), NEW, OLD)
