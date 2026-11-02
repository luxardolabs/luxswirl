"""A connection whose own statement_timeout is very short still opens in the suite.

The test-db-bound block asks every new Postgres connection where it is before
anything else runs on it. Before test-db-identity v2 that probe ran under the
connection's own `statement_timeout`, so an engine built with a short one (as
the VACUUM test builds with 100 ms) was refused as "could not say where it is"
whenever the probe outran it. v2 gives the probe its own timeout and retries a
cancelled first statement.
"""

from __future__ import annotations

import pytest
from fixtures._db_url import TEST_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("timeout_ms", ["5", "100"])
async def test_a_short_statement_timeout_is_not_refused(
    worker_db, timeout_ms: str
) -> None:
    # A new engine each time: each connect is a fresh server process, which is probed.
    for _ in range(10):
        engine = create_async_engine(
            TEST_DATABASE_URL,
            connect_args={"server_settings": {"statement_timeout": timeout_ms}},
        )
        try:
            async with engine.connect() as conn:
                timeout = (await conn.execute(text("SHOW statement_timeout"))).scalar()
        finally:
            await engine.dispose()
        # The probe's own timeout does not leak into the connection's.
        assert timeout == f"{timeout_ms}ms"
