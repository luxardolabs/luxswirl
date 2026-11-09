"""reset_invalid_rate_limits — a stored rate limit that is not a limit takes
the environment's.

A `security.*_rate_limit` setting saved before its value was validated can
hold text slowapi cannot parse (`10/15mins`), a non-string, or a limit no
request passes. The server already leaves the environment's limit in effect
for such a row and shows it as not applied. This revision writes the
environment's limit (SECURITY__<NAME>_RATE_LIMIT) into each one, with the
check every new value passes (`app/core/rate_limit_format.py`), and logs each
key it resets with the value it replaced. A valid limit is left as it is, so
the revision is safe to run again.

Downgrading changes nothing: the values it replaced were never in effect.

Revision ID: 8a4f2c6e1d37
Revises: 609ddde70f0b
Create Date: 2026-11-08

"""

import logging

import sqlalchemy as sa
from alembic import op
from app.core.config import settings
from app.core.rate_limit_format import check_rate_limit

revision = "8a4f2c6e1d37"
down_revision = "609ddde70f0b"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# Each rate-limit setting, by the name of its environment field on settings.security.
_RATE_LIMIT_KEYS = {
    "security.login_rate_limit": "login_rate_limit",
    "security.api_rate_limit": "api_rate_limit",
    "security.registration_rate_limit": "registration_rate_limit",
}

settings_table = sa.table(
    "settings",
    sa.column("key", sa.String()),
    sa.column("value", sa.JSON()),
)


def _is_limit(stored: object) -> bool:
    """Whether ``stored`` (the column's JSON: {"value": ..., "type": ...}, or a
    bare value written outside the app) holds a valid rate limit."""
    value = stored.get("value") if isinstance(stored, dict) else stored
    if not isinstance(value, str):
        return False
    try:
        check_rate_limit(value)
    except ValueError:
        return False
    return True


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(settings_table.c.key, settings_table.c.value).where(
            settings_table.c.key.in_(list(_RATE_LIMIT_KEYS))
        )
    ).all()
    for key, stored in rows:
        if _is_limit(stored):
            continue
        configured: str = getattr(settings.security, _RATE_LIMIT_KEYS[key])
        connection.execute(
            sa.update(settings_table)
            .where(settings_table.c.key == key)
            .values(value={"value": configured, "type": "string"})
        )
        logger.warning(
            "Reset %s to the environment's limit %r: the stored %r is not a rate limit",
            key,
            configured,
            stored,
        )


def downgrade() -> None:
    pass
