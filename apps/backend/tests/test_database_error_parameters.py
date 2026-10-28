"""A failed statement's bound values stay out of its error text, the log line
the handler writes, and the 500 it answers: a write's values include secrets
(a key's hash, a connection string, a script)."""

from __future__ import annotations

import logging

import pytest
from app.db.database import engine
from app.main import app, database_exception_handler
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

pytestmark = pytest.mark.integration

SECRET = "s3cr3t-bound-value-4217"


async def _failed_statement(db: AsyncSession) -> DBAPIError:
    """A statement that fails with SECRET bound; PostgreSQL's own message
    ("division by zero") does not quote it, so only a bind dump could."""
    with pytest.raises(DBAPIError) as caught:
        async with db.begin_nested():
            await db.execute(
                text("SELECT 1 / 0 WHERE CAST(:p AS text) IS NOT NULL"), {"p": SECRET}
            )
    return caught.value


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/admin/checks",
            "headers": [(b"accept", b"application/json")],
            "query_string": b"",
            "app": app,
        }
    )


def test_the_app_engine_hides_bound_parameters() -> None:
    assert engine.sync_engine.hide_parameters is True


async def test_a_failed_statement_s_text_carries_no_bound_value(
    db: AsyncSession,
) -> None:
    error = await _failed_statement(db)
    assert "division by zero" in str(error)
    assert SECRET not in str(error), str(error)


async def test_the_handler_logs_and_answers_without_the_bound_value(
    db: AsyncSession, app_logs: list[logging.LogRecord]
) -> None:
    error = await _failed_statement(db)
    try:
        raise error
    except DBAPIError as raised:
        response = await database_exception_handler(_request(), raised)

    assert response.status_code == 500
    assert SECRET not in bytes(response.body).decode()
    logged = [r for r in app_logs if r.getMessage() == "Database error"]
    assert logged, [r.getMessage() for r in app_logs]
    formatted = logging.Formatter().format(logged[0])
    assert "division by zero" in formatted
    assert SECRET not in formatted, formatted
