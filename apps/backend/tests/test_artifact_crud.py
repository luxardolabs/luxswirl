"""ArtifactCRUD deletes report how many rows went.

The deletes returned the driver's `rowcount` behind `type: ignore`; they now
count a data-modifying CTE. These pin the counts and that nothing outside the
criteria is touched.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.core.datetime_utils import utc_now
from app.crud.artifact_crud import ArtifactCRUD
from app.models.check_artifact_model import CheckArtifact
from app.models.enum_model import CheckArtifactType
from fixtures.factories import make_agent, make_check
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _check(db: AsyncSession) -> UUID:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    return check.id


def _artifact(check_id: UUID, *, age_days: int = 0, size: int = 10) -> CheckArtifact:
    created = utc_now() - timedelta(days=age_days)
    return CheckArtifact(
        id=uuid4(),
        check_id=check_id,
        check_result_id=uuid4(),
        check_result_timestamp=created,
        artifact_type=CheckArtifactType.SCREENSHOT,
        content_type="image/png",
        filename="shot.png",
        size_bytes=size,
        data=b"x" * size,
        created_at=created,
        updated_at=created,
    )


async def _ids(db: AsyncSession, check_id: UUID) -> set[UUID]:
    rows = await db.scalars(
        select(CheckArtifact.id).where(CheckArtifact.check_id == check_id)
    )
    return set(rows)


async def test_delete_older_than_counts_only_what_it_deleted(db: AsyncSession) -> None:
    check_id = await _check(db)
    keep = _artifact(check_id, age_days=1)
    db.add_all(
        [_artifact(check_id, age_days=40), _artifact(check_id, age_days=50), keep]
    )
    await db.flush()

    deleted = await ArtifactCRUD.delete_older_than(db, utc_now() - timedelta(days=30))

    assert deleted == 2
    assert await _ids(db, check_id) == {keep.id}


@pytest.mark.parametrize("keep", [0, 2])
async def test_delete_for_check_excluding_spares_the_kept_and_other_checks(
    db: AsyncSession, keep: int
) -> None:
    check_id, other_id = await _check(db), await _check(db)
    mine = [_artifact(check_id, age_days=d) for d in range(5)]
    theirs = _artifact(other_id)
    db.add_all([*mine, theirs])
    await db.flush()
    kept = {a.id for a in mine[:keep]}

    deleted = await ArtifactCRUD.delete_for_check_excluding(db, check_id, list(kept))

    assert deleted == 5 - keep
    assert await _ids(db, check_id) == kept
    assert await _ids(db, other_id) == {theirs.id}
