"""Database fixtures for integration tests.

All fixtures are function-scoped to avoid event loop conflicts with
pytest-asyncio auto mode. The test DB runs on tmpfs (RAM) so first-call
schema setup is fast (~200ms) and subsequent tests reuse the schema with
per-test transactional rollback.

Pattern mirrors luxwx/apps/backend/tests/fixtures/db.py.

Usage:
    @pytest.mark.integration
    async def test_something(db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        # Automatic rollback after test
"""

from __future__ import annotations

import contextlib
import os

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

# Import all model modules so they register with Base.metadata. Without this,
# Base.metadata.create_all() would only see whatever happened to be imported
# already, producing an empty schema.
import app.models  # noqa: F401
from app.models.base_model import Base
from fixtures._db_url import TEST_DATABASE_URL

# Module-level flag so we only create the schema once per pytest session.
_schema_created = False


# Hypertable definitions matching `app/db/database.py` init_db().
# (table, time_column, chunk_interval). Kept in sync manually because the
# init_db helper does many other things (continuous aggregates, retention
# policies, compression) we don't need for tests.
# `notification_logs` is NOT here, and must not be: app/db/database.py says in so
# many words that it is "a plain table (audit log, not metrics)" and never
# converts it. This list had drifted to include it, and because the conversion
# was wrapped in a blanket suppress the drift was invisible — TimescaleDB
# refuses it ("cannot create a unique index without the column sent_at"), and
# that swallowed refusal is what taught the fixture to swallow ALL of them.
_HYPERTABLES: tuple[tuple[str, str, str], ...] = (
    ("check_results", "timestamp", "1 day"),
    ("agent_metrics", "timestamp", "1 day"),
    ("check_artifacts", "created_at", "1 day"),
)


async def _create_schema(engine):
    """First-call schema setup: drop+recreate public, install timescaledb,
    create all model tables, convert hypertables.

    The engine is disposed between the drop and the CREATE EXTENSION on purpose.
    `DROP SCHEMA public CASCADE` takes the timescaledb extension with it, and
    TimescaleDB then refuses to be recreated on the same backend:

        extension "timescaledb" has already been loaded with another version
        HINT: Start a new session and execute CREATE EXTENSION as the first command.

    Disposing closes every pooled connection, so the next `begin()` gets a fresh
    backend and CREATE EXTENSION really is its first command. Without this the
    extension was never installed, `create_hypertable` did not exist as a
    function, and the schema came up with zero hypertables.
    """
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA public"))

    await engine.dispose()

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"))

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # NOT suppressed. A blanket `contextlib.suppress(Exception)` here meant every
    # conversion could fail and the schema came up with zero hypertables, which
    # only surfaced as three /database-health routes returning 500 with no
    # explanation anywhere. A conversion that fails is a broken fixture, not a
    # detail — so it fails here, where the reason is still in scope.
    for table, column, interval in _HYPERTABLES:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    SELECT create_hypertable(
                        '{table}', '{column}',
                        if_not_exists => TRUE,
                        chunk_time_interval => INTERVAL '{interval}',
                        migrate_data => TRUE
                    )
                    """
                )
            )

    # Continuous aggregates (check_results_5min, _hourly, _daily) are
    # production-only — they're brittle to recreate in tests (the CAGG
    # requires uncompressed hypertable state and timescale_view registration).
    # Tests that need 5min/hourly/daily rollups insert directly into the
    # underlying tables.


@pytest_asyncio.fixture
async def db():
    """Provide an isolated AsyncSession for each test.

    The schema is created on first use of the fixture in a pytest session.
    Each test runs inside a transaction that is rolled back at the end, so
    tests are isolated without paying the schema cost per-test.
    """
    global _schema_created  # noqa: PLW0603

    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        pool_size=5,
        max_overflow=5,
    )

    if not _schema_created:
        await _create_schema(engine)
        _schema_created = True

    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)

        yield session

        await session.close()
        if trans.is_active:
            await trans.rollback()

    await engine.dispose()
