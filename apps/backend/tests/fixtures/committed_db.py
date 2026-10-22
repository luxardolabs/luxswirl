"""Really-committing sessions, for durability locks and self-committing workers.

Every other test runs inside one rolled-back transaction (the test-db-bound
block in `apps/backend/conftest.py`). That cannot prove a write was COMMITTED:
inside the transaction nothing ever is, and a read on the same connection sees
uncommitted rows. The scheduler / cleanup / monitoring job bodies, and every
"writes are durable" lock, have to show the opposite — the exact axis a job
once failed on, where it "ran" but every write rolled back on session close.

``worker_db`` requests the block's ``real_commits`` fixture, so this test's
sessions commit for real on the test database, and the block empties every
table after it. It yields the app's own session maker — the same object
``worker_session()`` and ``get_db()`` use — so a test seeds through it, drives
the worker, and reads back from a FRESH session.

Nothing may be seeded for the whole run: the block's emptying would remove it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from app.db.database import AsyncSessionLocal
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def worker_db(
    real_commits: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The app's session maker, committing for real for this test."""
    yield AsyncSessionLocal
