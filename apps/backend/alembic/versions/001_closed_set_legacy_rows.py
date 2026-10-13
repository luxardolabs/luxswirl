"""closed_set_legacy_rows — bring rows stored before the closed-set typing into it.

Before HTTP method, DNS record type and artifact content type were enum-typed,
the API stored any string for them. A row outside the set now fails validation
on read: one such check stops its agent fetching any checks, and one such
artifact breaks its check's artifact listing.

- `check_config.http_method` / `record_type`: uppercased. A value still outside
  the set is cleared and the check disabled (it is never run with a guessed
  value), and its id and old value are logged.
- `check_artifacts` outside the content types the agent uploads are deleted: the
  console serves an artifact inline with its content type, so no other type can
  be served safely, and artifacts expire under retention anyway.

The sets are frozen here, not imported from the enum module, so a later enum edit
cannot change what this revision did. It also carries the content_type column's
new comment.

Revision ID: c24bfe691550
Revises: 43a72b7c97c8
Create Date: 2026-10-13

"""

import logging
from typing import Any

import sqlalchemy as sa
from alembic import op
from app.core.encrypted_types import EncryptedJSON
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection

revision = "c24bfe691550"
down_revision = "43a72b7c97c8"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)
DNS_RECORD_TYPES = frozenset(
    {"A", "AAAA", "CNAME", "MX", "TXT", "NS", "PTR", "SOA", "SRV", "CAA"}
)
ARTIFACT_CONTENT_TYPES = ("image/png", "application/zip")
CLOSED_SETS = (("http_method", HTTP_METHODS), ("record_type", DNS_RECORD_TYPES))

checks = sa.table(
    "checks",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("check_config", EncryptedJSON()),
    sa.column("enabled", sa.Boolean()),
)
check_artifacts = sa.table("check_artifacts", sa.column("content_type", sa.String()))


def _normalize(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The config with its closed-set fields brought into their sets, and the
    keys whose value could not be (cleared)."""
    fixed = dict(config)
    cleared: list[str] = []
    for key, allowed in CLOSED_SETS:
        if key not in fixed or fixed[key] is None:
            continue
        value = fixed[key]
        upper = value.strip().upper() if isinstance(value, str) else None
        if upper in allowed:
            fixed[key] = upper
        elif upper == "":
            del fixed[key]  # an empty form value was always "unset"
        else:
            del fixed[key]
            cleared.append(key)
    return fixed, cleared


def migrate(connection: Connection) -> None:
    """Normalize every check's config and delete off-set artifacts."""
    rows = connection.execute(
        sa.select(checks.c.id, checks.c.check_config).where(
            checks.c.check_config.is_not(None)
        )
    ).all()
    for check_id, config in rows:
        if not isinstance(config, dict):
            continue
        fixed, cleared = _normalize(config)
        if fixed == config:
            continue
        values: dict[str, Any] = {"check_config": fixed}
        if cleared:
            values["enabled"] = False
            logger.warning(
                "Disabled check %s: cleared %s outside its closed set",
                check_id,
                ", ".join(f"{k}={config[k]!r}" for k in cleared),
            )
        connection.execute(
            sa.update(checks).where(checks.c.id == check_id).values(**values)
        )

    deleted = connection.execute(
        sa.delete(check_artifacts).where(
            check_artifacts.c.content_type.not_in(ARTIFACT_CONTENT_TYPES)
        )
    ).rowcount
    if deleted:
        logger.warning(
            "Deleted %d artifact(s) whose content type is not one the agent uploads",
            deleted,
        )


_OLD_COMMENT = "MIME type: image/png, application/zip, etc"
_NEW_COMMENT = "MIME type the artifact is served with"


def _comment(new: str, old: str) -> None:
    op.alter_column(
        "check_artifacts",
        "content_type",
        existing_type=sa.String(length=50),
        existing_nullable=False,
        comment=new,
        existing_comment=old,
    )


def upgrade() -> None:
    migrate(op.get_bind())
    _comment(_NEW_COMMENT, _OLD_COMMENT)


def downgrade() -> None:
    """Restores the column comment. The data fixes are not undone: the values
    they replaced were invalid, and are not kept."""
    _comment(_OLD_COMMENT, _NEW_COMMENT)
