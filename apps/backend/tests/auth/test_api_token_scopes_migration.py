"""Migration 009 gives every token that already exists `*`, and leaves no default.

The suite builds its schema with create_all, so this runs the revision itself,
down and up again, around a token stored as an older version stored it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fixtures.factories import make_user
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "009_api_token_scopes.py"
)


def _run(connection: Any, step: str) -> None:
    spec = importlib.util.spec_from_file_location("rev009", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


async def test_existing_tokens_get_star_and_no_default_remains(
    db: AsyncSession,
) -> None:
    user = make_user(role="viewer")
    db.add(user)
    await db.flush()
    await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    token_id = uuid4()
    await db.execute(
        text(
            "INSERT INTO api_tokens (id, user_id, name, token_hash, token_prefix,"
            " created_at, updated_at) VALUES (:id, :u, 'legacy', :h, 'lsw_legacy__',"
            " now(), now())"
        ),
        {"id": token_id, "u": user.id, "h": uuid4().hex * 2},
    )
    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))

    scopes = (
        await db.execute(
            text("SELECT scopes FROM api_tokens WHERE id = :id"), {"id": token_id}
        )
    ).scalar_one()
    default = (
        await db.execute(
            text(
                "SELECT column_default, is_nullable, character_maximum_length"
                " FROM information_schema.columns"
                " WHERE table_name='api_tokens' AND column_name='scopes'"
            )
        )
    ).one()

    assert scopes == ["*"]
    assert default[0] is None
    assert default[1] == "NO"
