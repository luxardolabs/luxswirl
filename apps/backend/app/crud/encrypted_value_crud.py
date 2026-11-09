"""CRUD for the stored form of every encrypted column, read without decrypting.

The encrypted column types decrypt as they load, so a value the configured key
cannot read fails the whole statement. These reads cast each column to text in
SQL instead (a column still typed json, before revision 011, comes back as its
text too): the caller sees the stored ciphertext and judges it row by row.
`write_stored` writes such text back as it is given, already encrypted.
"""

from collections.abc import Sequence
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    String,
    Text,
    Uuid,
    cast,
    column,
    inspect,
    select,
    table,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.check_model import Check
from app.models.maintenance_job_model import MaintenanceJob

# The columns stored encrypted, by table, in the order a refusal names them.
CHECK_COLUMNS = ("target", "check_config", "connection_string_encrypted")
MAINTENANCE_JOB_COLUMNS = ("params",)


def _limit(model_column: ColumnElement[Any]) -> int | None:
    """The most characters ``model_column`` stores; None for unbounded text."""
    length = getattr(model_column.type, "length", None)
    return length if isinstance(length, int) else None


# The most characters each encrypted column stores (None: unbounded), by table.
COLUMN_LIMITS: dict[str, dict[str, int | None]] = {
    "checks": {name: _limit(Check.__table__.c[name]) for name in CHECK_COLUMNS},
    "maintenance_jobs": {
        name: _limit(MaintenanceJob.__table__.c[name])
        for name in MAINTENANCE_JOB_COLUMNS
    },
}

# The encrypted columns as plain text: a write through these stores exactly the
# value given, which the encrypted types would encrypt a second time.
_STORED_TABLES = {
    "checks": table(
        "checks", column("id", Uuid()), *(column(c, Text()) for c in CHECK_COLUMNS)
    ),
    "maintenance_jobs": table(
        "maintenance_jobs",
        column("id", Uuid()),
        *(column(c, Text()) for c in MAINTENANCE_JOB_COLUMNS),
    ),
}


class StoredRow(NamedTuple):
    """One row's id and its encrypted columns as stored (None where null)."""

    id: UUID
    values: tuple[str | None, ...]


def _has_columns(session: Session) -> bool:
    inspector = inspect(session.connection())
    for table_name, columns in (
        ("checks", CHECK_COLUMNS),
        ("maintenance_jobs", MAINTENANCE_JOB_COLUMNS),
    ):
        if not inspector.has_table(table_name):
            return False
        present = {found["name"] for found in inspector.get_columns(table_name)}
        if not set(columns) <= present:
            return False
    return True


def _text_columns(session: Session) -> frozenset[tuple[str, str]]:
    inspector = inspect(session.connection())
    found: set[tuple[str, str]] = set()
    for table_name, columns in (
        ("checks", CHECK_COLUMNS),
        ("maintenance_jobs", MAINTENANCE_JOB_COLUMNS),
    ):
        if not inspector.has_table(table_name):
            continue
        for described in inspector.get_columns(table_name):
            if described["name"] in columns and isinstance(described["type"], String):
                found.add((table_name, described["name"]))
    return frozenset(found)


class EncryptedValueCRUD:
    @staticmethod
    async def text_columns(db: AsyncSession) -> frozenset[tuple[str, str]]:
        """The encrypted columns, as (table, column), stored as text in the database
        now (before revision 011, maintenance_jobs.params is still json)."""
        return await db.run_sync(_text_columns)

    @staticmethod
    async def columns_exist(db: AsyncSession) -> bool:
        """Whether the database has every encrypted column (an empty database,
        or one at an old revision, does not yet)."""
        return await db.run_sync(_has_columns)

    @staticmethod
    async def check_page(
        db: AsyncSession, *, skip: UUID | None, limit: int
    ) -> Sequence[StoredRow]:
        """Up to ``limit`` checks after the id ``skip`` (the last of the page before), by id, each with
        ``CHECK_COLUMNS`` as stored."""
        stmt = select(
            Check.id,
            cast(Check.target, Text),
            cast(Check.check_config, Text),
            cast(Check.connection_string_encrypted, Text),
        ).order_by(Check.id)
        if skip is not None:
            stmt = stmt.where(Check.id > skip)
        result = await db.execute(stmt.limit(limit))
        return [StoredRow(row[0], tuple(row[1:])) for row in result.all()]

    @staticmethod
    async def job_page(
        db: AsyncSession, *, skip: UUID | None, limit: int
    ) -> Sequence[StoredRow]:
        """Up to ``limit`` maintenance jobs after the id ``skip``, by id, each with
        ``MAINTENANCE_JOB_COLUMNS`` as stored, whatever their status."""
        stmt = select(MaintenanceJob.id, cast(MaintenanceJob.params, Text)).order_by(
            MaintenanceJob.id
        )
        if skip is not None:
            stmt = stmt.where(MaintenanceJob.id > skip)
        result = await db.execute(stmt.limit(limit))
        return [StoredRow(row[0], (row[1],)) for row in result.all()]

    @staticmethod
    async def write_stored(
        db: AsyncSession, table_name: str, row_id: UUID, values: dict[str, str]
    ) -> None:
        """Store ``values`` (column: the text to store, already encrypted) in
        the row ``row_id`` of ``table_name`` ("checks" or "maintenance_jobs")."""
        stored = _STORED_TABLES[table_name]
        await db.execute(update(stored).where(stored.c.id == row_id).values(**values))
