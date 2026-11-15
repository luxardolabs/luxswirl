"""Migration 020 adds checks.ran_synthetic and marks every check that ran a script.

The suite's schema is already at head, so this runs the revision itself, down and
up again, over checks made before it, and reads what the upgrade marked.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fixtures.factories import make_agent, make_check, make_check_result
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "020_check_ran_synthetic.py"
)


def _run(connection: Any, step: str) -> None:
    spec = importlib.util.spec_from_file_location("rev020", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


async def test_the_upgrade_marks_each_check_that_is_or_was_synthetic(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    synthetic = make_check(agent_id=agent.id, check_type="synthetic")
    retyped = make_check(agent_id=agent.id, check_type="http")
    plain = make_check(agent_id=agent.id, check_type="http")
    db.add_all([synthetic, retyped, plain])
    await db.flush()
    db.add_all(
        [
            make_check_result(
                check_id=retyped.id,
                agent_id=agent.id,
                metrics=json.dumps({"synthetic": {"status": "failure"}}),
            ),
            make_check_result(
                check_id=plain.id,
                agent_id=agent.id,
                metrics=json.dumps({"response": {"status_code": 200}}),
            ),
        ]
    )
    await db.flush()
    ids = {synthetic.id: True, retyped.id: True, plain.id: False}

    await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))

    rows = (
        await db.execute(
            text("SELECT id, ran_synthetic FROM checks WHERE id = ANY(:ids)"),
            {"ids": list(ids)},
        )
    ).all()
    assert dict(rows) == ids
