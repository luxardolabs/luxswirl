"""Search over checks' names and targets.

`checks.target` is encrypted, each value under its own random IV, so SQL cannot
match it: the search reads the rows the other filters narrowed, decrypted, and
matches here. The match is a case-insensitive substring, with no wildcards.
"""

from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession


async def ids_matching_search(
    db: AsyncSession, narrowed: Select[UUID, str, str], search: str
) -> list[UUID]:
    """The ids among `narrowed`'s (id, display_name, target) rows that match `search`."""
    needle = search.casefold()
    rows = (await db.execute(narrowed)).all()
    return [
        check_id
        for check_id, name, target in rows
        if needle in (name or "").casefold() or needle in (target or "").casefold()
    ]
