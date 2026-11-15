"""Migration 021 adds checks.ran_synthetic and marks every check that ran a script.

The suite's schema is already at head, so this runs the revision itself, down and
up again, over checks made before it, and reads what the upgrade marked.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import uuid4
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.datetime_utils import utc_now
from app.models.check_artifact_model import CheckArtifact
from app.models.enum_model import CheckArtifactType
from fixtures.factories import make_agent, make_check, make_check_result
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "021_check_ran_synthetic.py"
)


def _run(connection: Any, step: str) -> None:
    spec = importlib.util.spec_from_file_location("rev021", _REVISION)
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


async def _upgrade_again(db: AsyncSession) -> None:
    await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))


async def _ran(db: AsyncSession, check_id: Any) -> bool:
    return bool(
        (
            await db.execute(
                text("SELECT ran_synthetic FROM checks WHERE id = :i"), {"i": check_id}
            )
        ).scalar_one()
    )


async def _http_check_with(
    db: AsyncSession, *, metrics: str | None, error: str | None = None
) -> Any:
    """An http check with one stored result, before the upgrade runs."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, check_type="http")
    db.add(check)
    await db.flush()
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            success=error is None,
            error=error,
            metrics=metrics,
        )
    )
    await db.flush()
    return check.id


async def test_backfill_control_plain_http_stays_unmarked(db: AsyncSession) -> None:
    metrics = {"response": {"status_code": 200, "headers": {"x-mode": "live"}}}
    check_id = await _http_check_with(db, metrics=json.dumps(metrics))

    await _upgrade_again(db)

    assert await _ran(db, check_id) is False


async def test_backfill_does_not_mark_http_check_whose_header_says_synthetic(
    db: AsyncSession,
) -> None:
    """An http check's metrics carry its response headers; a header value of
    `synthetic` is text in the stored JSON, not the run's synthetic section."""
    metrics = {
        "response": {"status_code": 200, "headers": {"x-traffic-type": "synthetic"}}
    }
    check_id = await _http_check_with(db, metrics=json.dumps(metrics))

    await _upgrade_again(db)

    assert await _ran(db, check_id) is False


@pytest.mark.parametrize(
    "metrics",
    [
        "not json at all",
        '{"response": {"latency": NaN}, "synthetic"',
        '"synthetic"',
        '["synthetic"]',
    ],
    ids=["text", "broken-json", "json-string", "json-array"],
)
async def test_backfill_reads_a_row_that_is_no_json_object_as_unmarked(
    db: AsyncSession, metrics: str
) -> None:
    check_id = await _http_check_with(db, metrics=metrics)

    await _upgrade_again(db)

    assert await _ran(db, check_id) is False


@pytest.mark.parametrize(
    "error",
    [
        "Script execution failed: TimeoutError: Page.goto: Timeout 30000ms exceeded",
        "Script compilation failed: SyntaxError: invalid syntax",
        "Playwright initialization failed: Error: browser closed",
        "Script execution timed out after 30s",
    ],
    ids=["execution", "compilation", "initialization", "timed-out"],
)
async def test_backfill_marks_a_retyped_check_whose_synthetic_runs_were_size_noted(
    db: AsyncSession, error: str
) -> None:
    """A synthetic run whose metrics outgrew the server's limit reached it as the
    agent's size note, with no synthetic section; its error still starts with the
    agent's own failure prefix."""
    check_id = await _http_check_with(
        db,
        metrics=json.dumps({"metrics_truncated": True, "metrics_original_chars": 9e4}),
        error=error,
    )

    await _upgrade_again(db)

    assert await _ran(db, check_id) is True


async def test_backfill_leaves_an_http_error_that_merely_mentions_a_script(
    db: AsyncSession,
) -> None:
    check_id = await _http_check_with(
        db,
        metrics=json.dumps({"response": {"status_code": 500}}),
        error="HTTP 500: Script execution failed: upstream",
    )

    await _upgrade_again(db)

    assert await _ran(db, check_id) is False


async def test_backfill_marks_a_synthetic_row_postgres_cannot_read_as_json(
    db: AsyncSession,
) -> None:
    """Python writes a NaN figure as `NaN`, which PostgreSQL's JSON refuses; the
    row still opens with the agent's synthetic key."""
    check_id = await _http_check_with(
        db, metrics='{"synthetic":{"status":"success","browser_load_time_ms":NaN}}'
    )

    await _upgrade_again(db)

    assert await _ran(db, check_id) is True


# What the agent writes when a script returns `{"status": "failure", "errors":
# [...]}`: the script's own text, with no prefix of the agent's.
SCRIPTS_OWN_ERROR = "Login failed after opening https://app.example.com/reset/Zt0k"
SIZE_NOTE = json.dumps({"metrics_truncated": True, "metrics_original_chars": 90000})


async def test_backfill_marks_a_retyped_check_that_still_holds_its_script(
    db: AsyncSession,
) -> None:
    """A check retyped before a retype cleared the script keeps it; its runs'
    error can be the script's own text, cut to the size note."""
    check_id = await _http_check_with(db, metrics=SIZE_NOTE, error=SCRIPTS_OWN_ERROR)
    await db.execute(
        text("UPDATE checks SET script_code = 'async def run_check(page): ...' "
             "WHERE id = :i"),
        {"i": check_id},
    )

    await _upgrade_again(db)

    assert await _ran(db, check_id) is True


async def test_backfill_marks_a_retyped_check_that_owns_an_artifact(
    db: AsyncSession,
) -> None:
    """Only a synthetic run uploads a screenshot or a trace."""
    check_id = await _http_check_with(db, metrics=SIZE_NOTE, error=SCRIPTS_OWN_ERROR)
    now = utc_now()
    db.add(
        CheckArtifact(
            id=uuid4(),
            check_id=check_id,
            check_result_id=uuid4(),
            check_result_timestamp=now,
            artifact_type=CheckArtifactType.SCREENSHOT,
            content_type="image/png",
            filename="final-state.png",
            size_bytes=4,
            data=b"\x89PNG",
            created_at=now,
            updated_at=now,
        )
    )
    await db.flush()

    await _upgrade_again(db)

    assert await _ran(db, check_id) is True


async def test_backfill_control_size_noted_http_run_with_neither_stays_unmarked(
    db: AsyncSession,
) -> None:
    check_id = await _http_check_with(db, metrics=SIZE_NOTE, error=SCRIPTS_OWN_ERROR)

    await _upgrade_again(db)

    assert await _ran(db, check_id) is False
