"""Committed-session fixture for self-committing background workers.

The `db` fixture (fixtures/db.py) wraps each test in a transaction that is rolled
back at the end — ideal for code that takes an injected session, but useless for
the scheduler / cleanup / monitoring job bodies, which open their OWN session via
the global ``worker_session()`` / ``get_session_maker()`` and COMMIT for real.

The only way to prove those functions actually persisted anything — the exact
axis LUXSWIRL-191 failed on, where the job "ran" but every write rolled back on
session close — is to let them commit against the test DB and then read it back
from a FRESH session. A rollback fixture cannot do that.

``worker_db`` binds ``app.db.database.async_session_maker`` to a test-engine maker
so ``worker_session()`` hits the test DB, and cleans up by TRUNCATE (there is no
outer transaction to roll back). Do NOT combine it with the `db` fixture in one
test — they manage isolation differently.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.database as _database
import fixtures.db as _dbfix

# Candidate tables worker-job tests touch. Only those that actually exist are
# truncated, so the list can stay generous without breaking on schema changes.
_WORKER_TABLES = (
    "job_executions",
    "job_configurations",
    "agent_metrics",
    "check_results",
    "check_artifacts",
    "notification_logs",
    "sessions",
    "checks",
    "agents",
)


async def _truncate(engine) -> None:
    async with engine.begin() as conn:
        existing = (
            (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename = ANY(:t)"
                    ),
                    {"t": list(_WORKER_TABLES)},
                )
            )
            .scalars()
            .all()
        )
        if existing:
            await conn.execute(text(f"TRUNCATE {', '.join(existing)} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def worker_db():
    """Test-bound session maker; ``worker_session()`` commits to the test DB.

    Yields the maker so a test can open FRESH sessions both to seed committed
    setup data and to read results back after driving a worker. Truncates worker
    tables before and after the test for isolation (commits are real here — there
    is no transaction to roll back).
    """
    engine = create_async_engine(_dbfix.TEST_DATABASE_URL, echo=False, pool_size=5, max_overflow=5)

    if not _dbfix._schema_created:
        await _dbfix._create_schema(engine)
        _dbfix._schema_created = True

    test_maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    saved = _database.async_session_maker
    _database.async_session_maker = test_maker
    await _truncate(engine)
    try:
        yield test_maker
    finally:
        _database.async_session_maker = saved
        await _truncate(engine)
        await engine.dispose()
