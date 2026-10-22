"""Database fixtures for integration tests.

Isolation is not this file's job any more. `apps/backend/conftest.py` carries
the fleet's test-db-bound block: for every test it opens one connection from the
app's own engine, begins a transaction, and binds the app's own session maker
(`app.db.database.AsyncSessionLocal`) to it with savepoints. Every session the
app opens joins that transaction (a route's `get_db`, `worker_session()`, a job,
anything that took the maker), an app `commit()` releases a savepoint, and the
whole lot is rolled back when the test ends. See `luxarch --playbook
test-isolation`.

So the `db` fixture is just a session from the app's maker, the same thing a
route gets. What a test writes through it is visible to a route the test calls,
and the reverse, because both run on the test's one connection.

Usage:
    @pytest.mark.integration
    async def test_something(db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        # Rolled back after the test
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from app.db.database import AsyncSessionLocal, init_timescale
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from fixtures._db_url import TEST_DATABASE_URL

def _migrate_to_head(url: str) -> None:
    """`alembic upgrade head` against the given database. Sync, so it runs in a
    thread: env.py drives the async engine with asyncio.run(), which cannot be
    called from inside a running loop."""
    cfg = Config("alembic.ini")
    # env.py prefers this over settings, so the migration lands in the throwaway
    # database rather than whichever one DATABASE__URL happens to name.
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


async def _create_schema(engine: AsyncEngine) -> None:
    """The schema, built the SAME two ways a container builds it.

    `alembic upgrade head` builds the tables and `init_timescale` converts the
    hypertables, which is exactly what the backend image does on boot (the CMD
    migrates, then main.py's lifespan calls init_db). Neither half is restated
    here: a second description of the schema is the thing that drifts.

    The engine is disposed between the drop and the CREATE EXTENSION on purpose.
    `DROP SCHEMA public CASCADE` takes the timescaledb extension with it, and
    TimescaleDB then refuses to be recreated on the same backend:

        extension "timescaledb" has already been loaded with another version
        HINT: Start a new session and execute CREATE EXTENSION as the first command.

    Disposing closes every pooled connection, so the next `begin()` gets a fresh
    backend and CREATE EXTENSION really is its first command.
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


async def _build_schema() -> None:
    # Its own engine, disposed before any test runs: no connection of it can
    # outlive this loop, and it is not the app's engine the block watches.
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    try:
        await _create_schema(engine)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    """Build the schema once per run, before the first test's transaction opens.

    DDL only: nothing a test needs is seeded here (the block empties every table
    after a `real_commits` test, and refuses a write outside a test's
    transaction), so every test seeds its own rows.
    """
    asyncio.run(_build_schema())
    yield


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """A session from the app's own maker: it joins the test's transaction."""
    async with AsyncSessionLocal() as session:
        yield session
