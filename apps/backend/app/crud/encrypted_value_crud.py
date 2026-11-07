"""CRUD for the stored form of every encrypted column, read without decrypting.

The encrypted column types decrypt as they load, so a value the configured key
cannot read fails the whole statement. These reads coerce each column to plain
text instead: the caller sees the stored ciphertext and judges it row by row.
"""

from collections.abc import Sequence
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import Text, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob

# The columns stored encrypted, by table, in the order a refusal names them.
CHECK_COLUMNS = ("target", "check_config", "connection_string_encrypted")
MAINTENANCE_JOB_COLUMNS = ("params",)


class StoredRow(NamedTuple):
    """One row's id and its encrypted columns as stored (None where null)."""

    id: UUID
    values: tuple[str | None, ...]


class EncryptedValueCRUD:
    @staticmethod
    async def check_page(
        db: AsyncSession, after: UUID | None, limit: int
    ) -> Sequence[StoredRow]:
        """Up to ``limit`` checks after ``after`` by id, each with
        ``CHECK_COLUMNS`` as stored."""
        stmt = select(
            Check.id,
            type_coerce(Check.target, Text),
            type_coerce(Check.check_config, Text),
            type_coerce(Check.connection_string_encrypted, Text),
        ).order_by(Check.id)
        if after is not None:
            stmt = stmt.where(Check.id > after)
        result = await db.execute(stmt.limit(limit))
        return [StoredRow(row[0], tuple(row[1:])) for row in result.all()]

    @staticmethod
    async def unfinished_job_page(
        db: AsyncSession, after: UUID | None, limit: int
    ) -> Sequence[StoredRow]:
        """Up to ``limit`` queued or running maintenance jobs after ``after`` by
        id, each with ``MAINTENANCE_JOB_COLUMNS`` as stored. A finished job's
        params are never read again, so they are not judged."""
        stmt = (
            select(MaintenanceJob.id, type_coerce(MaintenanceJob.params, Text))
            .where(
                MaintenanceJob.status.in_(
                    (
                        MaintenanceJobStatus.QUEUED.value,
                        MaintenanceJobStatus.RUNNING.value,
                    )
                )
            )
            .order_by(MaintenanceJob.id)
        )
        if after is not None:
            stmt = stmt.where(MaintenanceJob.id > after)
        result = await db.execute(stmt.limit(limit))
        return [StoredRow(row[0], (row[1],)) for row in result.all()]
