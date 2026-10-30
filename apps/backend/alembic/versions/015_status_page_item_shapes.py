"""status_page_item_shapes — bring every page's items into the admin API's shapes.

The web editor once stored a group's filter and checks as the browser sent them,
so a page could hold a group that breaks its render, the manage page's and the
public page's alike, and that the editor therefore cannot open to fix: a filter
of null or of another type, a filter agent_id that is not a UUID, a group's
checks that are not a list of check ids.

Each page's items are rebuilt in the shape the admin API stores, and no group
comes out showing a check it did not show before: every filter field narrows
what a group shows, so a field is never dropped on its own.

- A check item keeps its check_id as a canonical UUID; one without a UUID is
  dropped.
- A group keeps its name (one that is not 1-255 characters of plain text reads
  "Group"), collapsed (a boolean, else false), and its sort (one of the sort
  values, else manual / asc).
- A filter group (one whose filter is an object) keeps its filter when every
  field it sets is one GroupFilter takes: agent_id a UUID of 32 hex digits,
  plain or hyphenated 8-4-4-4-12, any case (stored canonical), check_type a check type, tags a list of at most 50
  plain-text tags of at most 100 characters, search plain text of at most 255
  characters. A field holding an empty value is unset, as the render reads it
  (an empty search is kept, as the admin API stores it). A key GroupFilter does
  not have is dropped: no render reads one. A filter with any field it cannot
  take becomes a group that lists no checks, so it shows nothing until an
  editor sets it again.
- Any other group lists checks: those of its checks (a list, or an object's
  keys, which the render read the same way) that are UUIDs, canonical, each
  once, at most 1000; anything else is none.
- An item of no known type is dropped. Items are numbered in their order.

Only a page whose items change is written, and each one is logged. The sets
are frozen here, not imported, so a later schema edit cannot change what this
revision did.

Revision ID: e1b41fd38554
Revises: 7c2e5a9d4b18
Create Date: 2026-10-30

"""

import logging
import re
import unicodedata
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection

revision = "e1b41fd38554"
down_revision = "7c2e5a9d4b18"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

CHECK_TYPES = frozenset(
    {"ping", "http", "tcp", "json", "dns", "mysql", "postgres", "synthetic"}
)
SORT_BY = frozenset({"manual", "name", "status", "latency", "uptime"})
SORT_DIRECTIONS = frozenset({"asc", "desc"})
REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
FILTER_FIELDS = ("agent_id", "check_type", "tags", "search")
MAX_GROUP_CHECKS = 1000
MAX_TAGS = 50
MAX_TAG_LENGTH = 100
MAX_TEXT_LENGTH = 255
STORED_UUID = re.compile(
    r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

status_pages = sa.table(
    "status_pages",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("items", postgresql.JSONB()),
)


def _uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _stored_uuid(value: Any) -> str | None:
    """A UUID of 32 hex digits, plain or hyphenated 8-4-4-4-12, any case, in its
    canonical form; any other value is not taken."""
    if not isinstance(value, str) or not STORED_UUID.fullmatch(value):
        return None
    return str(UUID(value))


def _text(value: Any, min_length: int, max_length: int) -> str | None:
    if not isinstance(value, str) or not min_length <= len(value) <= max_length:
        return None
    if any(unicodedata.category(c) in REFUSED_CATEGORIES for c in value):
        return None
    return value


def _one_of(value: Any, allowed: frozenset[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _filter(raw: dict[str, Any]) -> dict[str, Any] | None:
    """The filter as GroupFilter stores it, or None when a field it sets is not
    one GroupFilter takes."""
    fixed: dict[str, Any] = {"tags": []}
    for field in FILTER_FIELDS:
        value = raw.get(field)
        # The render reads an empty value as unset; an empty search is kept as
        # the admin API stores it.
        if not value and not (field == "search" and value == ""):
            continue
        if field == "agent_id":
            value = _stored_uuid(value)
        elif field == "check_type":
            value = value if isinstance(value, str) and value in CHECK_TYPES else None
        elif field == "tags":
            tags = value if isinstance(value, list) else None
            value = (
                tags
                if tags is not None
                and len(tags) <= MAX_TAGS
                and all(_text(t, 0, MAX_TAG_LENGTH) is not None for t in tags)
                else None
            )
        else:
            value = _text(value, 0, MAX_TEXT_LENGTH)
        if value is None:
            return None
        fixed[field] = value
    return fixed


def _checks(raw: Any) -> list[str]:
    # The render iterated an object's keys as the group's checks.
    listed = list(raw) if isinstance(raw, (list, dict)) else []
    ids = (_uuid(c) for c in listed)
    return list(dict.fromkeys(c for c in ids if c is not None))[:MAX_GROUP_CHECKS]


def _group(raw: dict[str, Any], order: int) -> dict[str, Any]:
    group: dict[str, Any] = {
        "type": "group",
        "name": _text(raw.get("name"), 1, MAX_TEXT_LENGTH) or "Group",
        "order": order,
        "collapsed": raw.get("collapsed") is True,
        "sort_by": _one_of(raw.get("sort_by"), SORT_BY, "manual"),
        "sort_direction": _one_of(raw.get("sort_direction"), SORT_DIRECTIONS, "asc"),
    }
    if isinstance(raw.get("filter"), dict):
        fixed = _filter(raw["filter"])
        if fixed is None:
            group["checks"] = []
        else:
            group["filter"] = fixed
    else:
        group["checks"] = _checks(raw.get("checks"))
    return group


def normalize(items: Any) -> list[dict[str, Any]]:
    """A page's items in the admin API's shapes, numbered in their order."""
    fixed: list[dict[str, Any]] = []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "check":
            if (check_id := _uuid(raw.get("check_id"))) is not None:
                fixed.append(
                    {"type": "check", "check_id": check_id, "order": len(fixed)}
                )
        elif raw.get("type") == "group":
            fixed.append(_group(raw, len(fixed)))
    return fixed


def migrate(connection: Connection) -> None:
    """Rewrite each page whose items are not already in shape."""
    rows = connection.execute(
        sa.select(status_pages.c.id, status_pages.c["items"])
    ).all()
    for page_id, items in rows:
        fixed = normalize(items)
        if fixed == items:
            continue
        connection.execute(
            sa.update(status_pages)
            .where(status_pages.c.id == page_id)
            .values(items=fixed)
        )
        logger.warning(
            "Rewrote status page %s: %d stored item(s) became %d in the admin "
            "API's shapes",
            page_id,
            len(items) if isinstance(items, list) else 0,
            len(fixed),
        )


def upgrade() -> None:
    migrate(op.get_bind())


def downgrade() -> None:
    """The items are not restored: the values replaced were ones no page could
    render, and are not kept."""
