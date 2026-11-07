"""FieldEncryptionCoreService: the startup check that the configured
field-encryption key reads every stored encrypted value.

A server started under the wrong SECURITY__FIELD_ENCRYPTION_KEY would serve
500s on every read of a check and write new rows the right key then refuses.
So before anything reads or writes, every encrypted value a read can reach
(each check's columns, each queued or running maintenance job's params) is
decrypted, and one that does not decrypt stops the startup with its table,
column and row named.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encrypted_types import decrypts_or_is_plaintext
from app.core.exceptions import UnreadableEncryptedValuesException
from app.crud.encrypted_value_crud import (
    CHECK_COLUMNS,
    MAINTENANCE_JOB_COLUMNS,
    EncryptedValueCRUD,
    StoredRow,
)
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.field_encryption")

# Rows read per statement, and the most a refusal names.
_PAGE = 1000
_NAMED = 20

_REPAIR = (
    "Set SECURITY__FIELD_ENCRYPTION_KEY to the key this database was written "
    "with and start again. If the right key still names rows, they were "
    "written while another key was set: remove each one (see 'Server refuses "
    "to start: encrypted values do not decrypt' in "
    "docs/guides/troubleshooting.md)."
)


class _Tally:
    """What the configured key read, and the values it could not."""

    def __init__(self) -> None:
        self.readable = 0
        self.unreadable: list[str] = []

    def judge(self, table: str, columns: tuple[str, ...], row: StoredRow) -> None:
        for column, stored in zip(columns, row.values, strict=True):
            if stored is None:
                continue
            if decrypts_or_is_plaintext(stored):
                self.readable += 1
            else:
                self.unreadable.append(f"{table}.{column} id={row.id}")

    def refusal(self) -> str:
        """The refusal's message: the verdict, the values named, the repair."""
        count = len(self.unreadable)
        if self.readable == 0:
            verdict = (
                "SECURITY__FIELD_ENCRYPTION_KEY is not this database's key: "
                f"none of the {count} stored encrypted values decrypt under it"
            )
        else:
            verdict = (
                f"{count} stored encrypted values do not decrypt under "
                f"SECURITY__FIELD_ENCRYPTION_KEY ({self.readable} do)"
            )
        named = ", ".join(self.unreadable[:_NAMED])
        if count > _NAMED:
            named += f", and {count - _NAMED} more"
        return f"{verdict}: {named}. {_REPAIR}"


class FieldEncryptionCoreService:
    @staticmethod
    async def require_readable(db: AsyncSession) -> None:
        """Refuse when the configured key cannot read a stored encrypted value.

        Raises:
            UnreadableEncryptedValuesException: Some stored value does not
                decrypt under the configured key. The message names up to 20
                of them as ``table.column id=<row>``, how many there are, and
                whether the key read any value at all (none read: it is not
                this database's key).
        """
        tally = _Tally()
        after: UUID | None = None
        while page := await EncryptedValueCRUD.check_page(db, after, _PAGE):
            for row in page:
                tally.judge("checks", CHECK_COLUMNS, row)
            after = page[-1].id
        after = None
        while page := await EncryptedValueCRUD.unfinished_job_page(db, after, _PAGE):
            for row in page:
                tally.judge("maintenance_jobs", MAINTENANCE_JOB_COLUMNS, row)
            after = page[-1].id

        if not tally.unreadable:
            return
        message = tally.refusal()
        logger.error(
            "Startup refused: stored encrypted values do not decrypt",
            extra={"detail": message},
        )
        raise UnreadableEncryptedValuesException(message)
