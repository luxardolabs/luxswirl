"""A connection whose own statement_timeout is very short still opens in the suite.

The test-db-bound block asks every new Postgres connection where it is before
anything else runs on it. Before test-db-identity v2 that probe ran under the
connection's own `statement_timeout`, so an engine built with a short one (as
the VACUUM test builds with 100 ms) was refused as "could not say where it is"
whenever the probe outran it. The probe has its own timeout and retries a
cancelled first statement.

SQLAlchemy's own first-connect statements (`select current_schema()`) run under
the connection's timeout, outside the probe, so on a loaded machine one can be
cancelled at 5 ms. Such a connect is tried again: it says nothing about the
probe, whose refusal is an AssertionError (recorded for the whole run), never a
cancelled statement.
"""

from __future__ import annotations

import asyncpg
import pytest
from fixtures._db_url import TEST_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

CONNECTS = 10
TRIES_PER_CONNECT = 20


def _cancelled_by_timeout(error: DBAPIError) -> bool:
    """The statement was cancelled by the connection's own statement_timeout."""
    cause = error.orig.__cause__ if error.orig is not None else None
    return isinstance(cause, asyncpg.exceptions.QueryCanceledError)


async def _statement_timeout_of_a_new_connection(timeout_ms: str) -> str | None:
    """Open a fresh connection with the timeout and read it back, or None if cancelled."""
    # A new engine each time: each connect is a fresh server process, which is probed.
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"server_settings": {"statement_timeout": timeout_ms}},
    )
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("SHOW statement_timeout"))).scalar()
    except DBAPIError as error:
        if _cancelled_by_timeout(error):
            return None
        raise
    finally:
        await engine.dispose()


@pytest.mark.parametrize("timeout_ms", ["5", "100"])
async def test_a_short_statement_timeout_is_not_refused(
    worker_db, timeout_ms: str
) -> None:
    for _ in range(CONNECTS):
        for _ in range(TRIES_PER_CONNECT):
            timeout = await _statement_timeout_of_a_new_connection(timeout_ms)
            if timeout is not None:
                break
        # The probe's own timeout does not leak into the connection's.
        assert timeout == f"{timeout_ms}ms"
