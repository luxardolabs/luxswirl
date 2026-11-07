"""FieldEncryptionCoreService: the check that the configured field-encryption
key reads every stored encrypted value, run before migrations and at startup.

A server started under the wrong SECURITY__FIELD_ENCRYPTION_KEY would serve
500s on every read of a check and write new rows the right key then refuses,
and a migration run under it would rewrite values under it. So before either
writes anything, every stored encrypted value (each check's columns, each
maintenance job's params) is decrypted, and one that does not decrypt stops
it with its table, column and row named.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encrypted_types import decrypts, is_token
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

_GUIDE = (
    "(see 'Server refuses to start: encrypted values do not decrypt' in "
    "docs/guides/troubleshooting.md)"
)
# The key is not, or most likely not, this database's: nothing is removed.
_SET_THE_KEY = (
    "Set SECURITY__FIELD_ENCRYPTION_KEY to the key this database was written "
    f"with and start again; remove no rows under this key {_GUIDE}."
)
# The key decrypts most values: the rest were written under another key.
_REMOVE_THE_ROWS = (
    "If this is the key the server has always used, these rows were written "
    f"while another key was set and no key reads both: remove each one {_GUIDE}."
)


class _Tally:
    """The values the configured key decrypted, and the tokens it could not.

    A value stored before encryption (not shaped as a token) reads under any
    key, so it counts toward neither: only a decrypted token shows the key is
    this database's.
    """

    def __init__(self) -> None:
        self.decrypted = 0
        self.unreadable: list[str] = []

    def judge(self, table: str, columns: tuple[str, ...], row: StoredRow) -> None:
        for column, stored in zip(columns, row.values, strict=True):
            if stored is None:
                continue
            if decrypts(stored):
                self.decrypted += 1
            elif is_token(stored):
                self.unreadable.append(f"{table}.{column} id={row.id}")

    def refusal(self) -> str:
        """The refusal's message: the verdict, the values named, the repair.

        Removing the named rows is advised only when the key decrypts more
        values than it cannot: a key that wrote a few rows during a wrong-key
        window decrypts those few, and the rows it names are the good ones.
        """
        count = len(self.unreadable)
        if self.decrypted == 0:
            verdict = (
                "SECURITY__FIELD_ENCRYPTION_KEY is not this database's key: "
                f"none of the {count} stored encrypted values decrypt under it"
            )
            repair = _SET_THE_KEY
        elif self.decrypted <= count:
            verdict = (
                "SECURITY__FIELD_ENCRYPTION_KEY is most likely not this "
                f"database's key: only {self.decrypted} of the "
                f"{self.decrypted + count} stored encrypted values decrypt under it"
            )
            repair = _SET_THE_KEY
        else:
            verdict = (
                f"{count} stored encrypted values do not decrypt under "
                f"SECURITY__FIELD_ENCRYPTION_KEY ({self.decrypted} do)"
            )
            repair = _REMOVE_THE_ROWS
        named = ", ".join(self.unreadable[:_NAMED])
        if count > _NAMED:
            named += f", and {count - _NAMED} more"
        return f"{verdict}: {named}. {repair}"


class FieldEncryptionCoreService:
    @staticmethod
    async def require_readable(db: AsyncSession) -> None:
        """Refuse when the configured key cannot read a stored encrypted value.

        Raises:
            UnreadableEncryptedValuesException: Some stored value does not
                decrypt under the configured key. The message names up to 20
                of them as ``table.column id=<row>``, how many there are, and
                whether the key decrypted any value at all (none: it is not
                this database's key).
        """
        tally = _Tally()
        after: UUID | None = None
        while page := await EncryptedValueCRUD.check_page(db, after, _PAGE):
            for row in page:
                tally.judge("checks", CHECK_COLUMNS, row)
            after = page[-1].id
        after = None
        while page := await EncryptedValueCRUD.job_page(db, after, _PAGE):
            for row in page:
                tally.judge("maintenance_jobs", MAINTENANCE_JOB_COLUMNS, row)
            after = page[-1].id

        if not tally.unreadable:
            return
        message = tally.refusal()
        logger.error(
            "Refused: stored encrypted values do not decrypt",
            extra={"detail": message},
        )
        raise UnreadableEncryptedValuesException(message)

    @staticmethod
    async def require_readable_before_migrating(db: AsyncSession) -> None:
        """``require_readable``, for a migration run: a database without the
        encrypted columns yet (empty, or at an old revision) holds nothing to
        judge.

        Raises:
            UnreadableEncryptedValuesException: As ``require_readable``.
        """
        if await EncryptedValueCRUD.columns_exist(db):
            await FieldEncryptionCoreService.require_readable(db)
