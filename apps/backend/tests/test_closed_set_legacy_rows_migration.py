"""The closed-set legacy-row migration brings stored rows into their sets.

Rows an older version stored with a value outside the sets the schemas now
enforce fail on read (the agent's check fetch, an artifact listing). Revision
c24bfe691550 uppercases what it can, clears and disables a check it cannot fix,
and deletes artifacts with a content type the console cannot serve safely. The
test suite's schema is already at head, so this runs the revision's
`migrate` directly against rows shaped like an older version's.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.core.datetime_utils import utc_now
from app.models.check_artifact_model import CheckArtifact
from app.models.check_model import Check
from fixtures.factories import make_agent, make_check
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "001_closed_set_legacy_rows.py"
)


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("closed_set_legacy_rows", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _migrate(db: AsyncSession) -> None:
    revision = _revision()
    await db.run_sync(lambda session: revision.migrate(session.connection()))
    db.expire_all()


async def _check(db: AsyncSession, agent_id, config: dict[str, Any]) -> UUID:
    check = make_check(
        agent_id=agent_id,
        check_type="http",
        target="https://legacy.test",
        check_config=config,
        enabled=True,
    )
    db.add(check)
    await db.flush()
    return check.id


async def test_stored_check_config_is_brought_into_its_sets(db: AsyncSession) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    lower = await _check(
        db, agent.id, {"http_method": "options", "expected_status": 200}
    )
    clean = await _check(db, agent.id, {"http_method": "GET"})
    bad_method = await _check(
        db, agent.id, {"http_method": "FETCH", "verify_ssl": False}
    )
    bad_record = await _check(
        db, agent.id, {"record_type": "ANY", "nameserver": "1.1.1.1"}
    )
    empty = await _check(db, agent.id, {"http_method": ""})

    await _migrate(db)

    def stored(check: Check) -> tuple[dict[str, Any] | None, bool]:
        return check.check_config, check.enabled

    rows = {
        c.id: c
        for c in await db.scalars(
            select(Check).where(
                Check.id.in_([lower, clean, bad_method, bad_record, empty])
            )
        )
    }
    assert stored(rows[lower]) == (
        {"http_method": "OPTIONS", "expected_status": 200},
        True,
    )
    assert stored(rows[clean]) == ({"http_method": "GET"}, True)
    assert stored(rows[bad_method]) == ({"verify_ssl": False}, False)
    assert stored(rows[bad_record]) == ({"nameserver": "1.1.1.1"}, False)
    assert stored(rows[empty]) == ({}, True)


async def test_artifacts_the_console_cannot_serve_are_deleted(db: AsyncSession) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, check_type="synthetic")
    db.add(check)
    await db.flush()
    check_id = check.id
    now = utc_now()
    for content_type in ("image/png", "application/zip", "video/webm", "text/html"):
        await db.execute(
            text(
                "INSERT INTO check_artifacts (id, check_id, check_result_id,"
                " check_result_timestamp, artifact_type, content_type, filename,"
                " size_bytes, data, created_at, updated_at) VALUES (:id, :cid, :rid,"
                " :ts, 'screenshot', :ct, 'x.bin', 1, :data, :ts, :ts)"
            ),
            {
                "id": uuid4(),
                "cid": check_id,
                "rid": uuid4(),
                "ts": now,
                "ct": content_type,
                "data": b"x",
            },
        )

    await _migrate(db)

    rows = await db.scalars(
        select(CheckArtifact.content_type).where(CheckArtifact.check_id == check_id)
    )
    assert sorted(rows) == ["application/zip", "image/png"]
