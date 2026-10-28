"""A failed statement's bound values stay out of its error text, the log line
the handler writes, and the 500 it answers: a write's values include secrets
(a key's hash, a connection string, a script). PostgreSQL's own DETAIL (a
duplicated key, a failing row) and asyncpg's refused argument quote values too,
so the log line and the 500 are scrubbed of them."""

from __future__ import annotations

import logging

import pytest
from app.db.database import engine
from app.main import app, database_exception_handler
from app.models.registration_key_model import RegistrationKey
from app.shared.log_redaction import scrub_text
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


async def _written(
    error: DBAPIError, app_logs: list[logging.LogRecord]
) -> tuple[str, str]:
    """(the 500's body, the "Database error" line as the log writes it)."""
    try:
        raise error
    except DBAPIError as raised:
        response = await database_exception_handler(_request(), raised)
    logged = [r for r in app_logs if r.getMessage() == "Database error"]
    assert logged, [r.getMessage() for r in app_logs]
    return (
        bytes(response.body).decode(),
        scrub_text(logging.Formatter().format(logged[0])),
    )


async def test_a_duplicated_key_s_value_reaches_neither_log_nor_answer(
    db: AsyncSession, app_logs: list[logging.LogRecord]
) -> None:
    """A unique violation's DETAIL quotes the key: `Key (key_hash)=(...)`."""
    db.add(RegistrationKey(name="a", key_hash=SECRET))
    await db.flush()
    with pytest.raises(DBAPIError) as caught:
        async with db.begin_nested():
            db.add(RegistrationKey(name="b", key_hash=SECRET))
            await db.flush()

    body, line = await _written(caught.value, app_logs)
    assert "key_hash" in line
    assert SECRET not in body, body
    assert SECRET not in line, line


async def test_a_failing_row_s_values_reach_neither_log_nor_answer(
    db: AsyncSession, app_logs: list[logging.LogRecord]
) -> None:
    """A NOT NULL violation's DETAIL quotes the whole row being written."""
    with pytest.raises(DBAPIError) as caught:
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO registration_keys (id, name, key_hash)"
                    " VALUES (gen_random_uuid(), NULL, :h)"
                ),
                {"h": SECRET},
            )

    body, line = await _written(caught.value, app_logs)
    assert "Failing row contains" in line
    assert SECRET not in body, body
    assert SECRET not in line, line


async def test_a_refused_argument_s_value_reaches_neither_log_nor_answer(
    db: AsyncSession, app_logs: list[logging.LogRecord]
) -> None:
    """asyncpg quotes an argument it cannot encode, in the error text itself."""
    with pytest.raises(DBAPIError) as caught:
        async with db.begin_nested():
            await db.execute(
                text("SELECT 1 FROM registration_keys WHERE id = :p"), {"p": SECRET}
            )

    body, line = await _written(caught.value, app_logs)
    assert "query argument" in line
    assert SECRET not in body, body
    assert SECRET not in line, line
