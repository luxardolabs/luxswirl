"""Search over checks' names and targets.

`checks.target` is encrypted, each value under its own random IV, so SQL cannot
match it: the search reads the rows the other filters narrowed, decrypted, and
matches here. The match is a case-insensitive substring, with no wildcards.

A target is matched as the reader is shown it: with its credentials masked for an
editor or admin, cut to its scheme, host and port for anyone else. A match on more
than the reader is shown would answer whether a guess at what is hidden is right.
"""

from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credential_mask import mask_credentials
from app.core.target_origin import target_origin


async def ids_matching_search(
    db: AsyncSession,
    narrowed: Select[UUID, str, str],
    search: str,
    *,
    full_target: bool,
) -> list[UUID]:
    """The ids among `narrowed`'s (id, display_name, target) rows that match `search`,
    on the masked target when `full_target` (the reader holds it), else on its
    scheme, host and port."""
    needle = search.casefold()
    shown = mask_credentials if full_target else target_origin
    rows = (await db.execute(narrowed)).all()
    return [
        check_id
        for check_id, name, target in rows
        if needle in (name or "").casefold()
        or needle in (shown(target) or "").casefold()
    ]
