"""CRUD for the stored form of every encrypted column, read without decrypting.

The encrypted column types decrypt as they load, so a value the configured key
cannot read fails the whole statement. These reads cast each column to text in
SQL instead (a column still typed json, before revision 011, comes back as its
text too): the caller sees the stored ciphertext and judges it row by row.
"""

from collections.abc import Sequence
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import Text, cast, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.check_model import Check
from app.models.maintenance_job_model import MaintenanceJob

# The columns stored encrypted, by table, in the order a refusal names them.
CHECK_COLUMNS = ("target", "check_config", "connection_string_encrypted")
MAINTENANCE_JOB_COLUMNS = ("params",)


class StoredRow(NamedTuple):
    """One row's id and its encrypted columns as stored (None where null)."""

    id: UUID
    values: tuple[str | None, ...]


def _has_columns(session: Session) -> bool:
    inspector = inspect(session.connection())
    for table, columns in (
        ("checks", CHECK_COLUMNS),
        ("maintenance_jobs", MAINTENANCE_JOB_COLUMNS),
    ):
        if not inspector.has_table(table):
            return False
        present = {column["name"] for column in inspector.get_columns(table)}
        if not set(columns) <= present:
            return False
    return True


class EncryptedValueCRUD:
    @staticmethod
    async def columns_exist(db: AsyncSession) -> bool:
        """Whether the database has every encrypted column (an empty database,
        or one at an old revision, does not yet)."""
        return await db.run_sync(_has_columns)

    @staticmethod
    async def check_page(
        db: AsyncSession, after: UUID | None, limit: int
    ) -> Sequence[StoredRow]:
        """Up to ``limit`` checks after ``after`` by id, each with
        ``CHECK_COLUMNS`` as stored."""
        stmt = select(
            Check.id,
            cast(Check.target, Text),
            cast(Check.check_config, Text),
            cast(Check.connection_string_encrypted, Text),
        ).order_by(Check.id)
        if after is not None:
            stmt = stmt.where(Check.id > after)
        result = await db.execute(stmt.limit(limit))
        return [StoredRow(row[0], tuple(row[1:])) for row in result.all()]

    @staticmethod
    async def job_page(
        db: AsyncSession, after: UUID | None, limit: int
    ) -> Sequence[StoredRow]:
        """Up to ``limit`` maintenance jobs after ``after`` by id, each with
        ``MAINTENANCE_JOB_COLUMNS`` as stored, whatever their status."""
        stmt = select(MaintenanceJob.id, cast(MaintenanceJob.params, Text)).order_by(
            MaintenanceJob.id
        )
        if after is not None:
            stmt = stmt.where(MaintenanceJob.id > after)
        result = await db.execute(stmt.limit(limit))
        return [StoredRow(row[0], (row[1],)) for row in result.all()]
