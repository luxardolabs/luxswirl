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

import asyncio

import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.database import init_timescale
from fixtures._db_url import TEST_DATABASE_URL

# Module-level flag so we only create the schema once per pytest session.
_schema_created = False


def _migrate_to_head(url: str) -> None:
    """`alembic upgrade head` against the given database. Sync, so it runs in a
    thread: env.py drives the async engine with asyncio.run(), which cannot be
    called from inside a running loop."""
    cfg = Config("alembic.ini")
    # env.py prefers this over settings, so the migration lands in the throwaway
    # database rather than whichever one DATABASE__URL happens to name.
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


async def _create_schema(engine):
    """First-call schema setup — the SAME two steps a container performs.

    `alembic upgrade head` builds the tables and `init_timescale` converts the
    hypertables, which is exactly what the backend image does on boot (the CMD
    migrates, then main.py's lifespan calls init_db). Neither half is restated
    here: a second description of the schema is the thing that drifts, and this
    file used to carry one — `Base.metadata.create_all` plus a hand-maintained
    hypertable list. create_all builds from the MODELS while a deploy builds
    from the CHAIN, so the suite could pass on a schema no deploy would ever
    produce; and the hypertable list had drifted to name a table init_db
    deliberately leaves alone.

    The engine is disposed between the drop and the CREATE EXTENSION on purpose.
    `DROP SCHEMA public CASCADE` takes the timescaledb extension with it, and
    TimescaleDB then refuses to be recreated on the same backend:

        extension "timescaledb" has already been loaded with another version
        HINT: Start a new session and execute CREATE EXTENSION as the first command.

    Disposing closes every pooled connection, so the next `begin()` gets a fresh
    backend and CREATE EXTENSION really is its first command. Without it the
    extension is never installed and `create_hypertable` does not exist as a
    function.
    """
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA public"))

    await engine.dispose()

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"))

    await asyncio.to_thread(_migrate_to_head, TEST_DATABASE_URL)
    await init_timescale(engine)


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
