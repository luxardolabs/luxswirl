"""FieldEncryptionCoreService: the check that the configured field-encryption
key reads every stored encrypted value, run before migrations and at startup.

A server started under the wrong SECURITY__FIELD_ENCRYPTION_KEY would serve
500s on every read of a check and write new rows the right key then refuses,
and a migration run under it would rewrite values under it. So before either
writes anything, every stored encrypted value (each check's columns, each
maintenance job's params) is decrypted, and one that does not decrypt stops
it with its table, column and row named.

When two keys wrote the database, `reencrypt_from` is the repair: given the
other key, it re-encrypts every value that key wrote under the configured one
(and encrypts any value still stored in plaintext), refusing, with nothing
written, while some value decrypts under neither.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.encrypted_types import (
    decrypt_with,
    decrypts,
    encrypt_for_storage,
    fernet_key_error,
    is_token,
)
from app.core.exceptions import (
    UnreadableEncryptedValuesException,
    ValidationException,
)
from app.crud.encrypted_value_crud import (
    CHECK_COLUMNS,
    COLUMN_LIMITS,
    MAINTENANCE_JOB_COLUMNS,
    EncryptedValueCRUD,
    StoredRow,
)
from app.schemas.field_encryption_schema import ReencryptionReport
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.field_encryption")

# Rows read per statement, and the most a refusal names.
_PAGE = 1000
_NAMED = 20

_GUIDE = (
    "(see 'Server refuses to start: encrypted values do not decrypt' in "
    "docs/guides/troubleshooting.md)"
)
# The key decrypts nothing stored: it is not this database's.
_SET_THE_KEY = (
    "Set SECURITY__FIELD_ENCRYPTION_KEY to the key this database was written "
    f"with and start again {_GUIDE}."
)
# The key decrypts some values and not others: two keys wrote this database,
# and nothing here can tell which is the right one.
_TWO_KEYS = (
    "Two keys wrote this database. Start under the key the server has always "
    "used; the values it still names were written under the other one, which "
    "still reads them: re-encrypt them with `python -m app.scripts.reencrypt_fields` "
    f"{_GUIDE}."
)


def _named(names: list[str]) -> str:
    shown = ", ".join(names[:_NAMED])
    if len(names) > _NAMED:
        shown += f", and {len(names) - _NAMED} more"
    return shown


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

        Only a key that decrypts nothing is judged: when it decrypts some
        values and not others, which key is right is the operator's to say
        (a count of rows or values cannot), so the message states what each
        key reads and advises removing nothing.
        """
        count = len(self.unreadable)
        if self.decrypted == 0:
            verdict = (
                "SECURITY__FIELD_ENCRYPTION_KEY is not this database's key: "
                f"none of the {count} stored encrypted values decrypt under it"
            )
            repair = _SET_THE_KEY
        else:
            verdict = (
                f"{count} stored encrypted values do not decrypt under "
                f"SECURITY__FIELD_ENCRYPTION_KEY ({self.decrypted} do)"
            )
            repair = _TWO_KEYS
        return f"{verdict}: {_named(self.unreadable)}. {repair}"


_Page = Callable[..., Awaitable[Sequence[StoredRow]]]


async def _stored_rows(
    db: AsyncSession,
) -> AsyncIterator[tuple[str, tuple[str, ...], StoredRow]]:
    """Every row holding encrypted columns, as (table, its columns, the row)."""
    tables: tuple[tuple[str, tuple[str, ...], _Page], ...] = (
        ("checks", CHECK_COLUMNS, EncryptedValueCRUD.check_page),
        ("maintenance_jobs", MAINTENANCE_JOB_COLUMNS, EncryptedValueCRUD.job_page),
    )
    for table, columns, read_page in tables:
        after: UUID | None = None
        while page := await read_page(db, skip=after, limit=_PAGE):
            for row in page:
                yield table, columns, row
            after = page[-1].id


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
        async for table, columns, row in _stored_rows(db):
            tally.judge(table, columns, row)

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

    @staticmethod
    async def reencrypt_from(db: AsyncSession, other_key: str) -> ReencryptionReport:
        """Re-encrypt under the configured key every stored value ``other_key``
        wrote, and encrypt every value stored in plaintext.

        Every value is judged before anything is written: one that decrypts
        under neither key refuses the run, and nothing changes. A value the
        configured key decrypts is left byte for byte, so a second run writes
        nothing. A plaintext value whose token would not fit its column is left
        and named.

        Raises:
            ValidationException: ``other_key`` is not a Fernet key, or is the
                configured key.
            UnreadableEncryptedValuesException: Some stored value decrypts
                under neither key; the message names up to 20 of them.
        """
        other_key = other_key.strip()
        refused = fernet_key_error(other_key)
        if refused is not None:
            raise ValidationException(f"The other key is refused: {refused}")
        if other_key == settings.security.field_encryption_key.strip():
            raise ValidationException(
                "The other key is SECURITY__FIELD_ENCRYPTION_KEY itself: give "
                "the key the named values were written under"
            )

        already = 0
        reencrypted: list[str] = []
        encrypted: list[str] = []
        too_long: list[str] = []
        unreadable: list[str] = []
        writes: list[tuple[str, UUID, dict[str, str]]] = []
        text_columns = await EncryptedValueCRUD.text_columns(db)
        async for table, columns, row in _stored_rows(db):
            values: dict[str, str] = {}
            for column, stored in zip(columns, row.values, strict=True):
                if stored is None:
                    continue
                name = f"{table}.{column} id={row.id}"
                if (table, column) not in text_columns:
                    continue  # not yet text: the revision that makes it text encrypts it
                if decrypts(stored):
                    already += 1
                    continue
                # Tried before the token shape: Fernet skips what is not base64, so a
                # token stored wrapped or quoted still decrypts, and is no plaintext.
                other_plaintext = decrypt_with(other_key, stored)
                if other_plaintext is not None:
                    plaintext = other_plaintext
                    outcome = reencrypted
                elif is_token(stored):
                    unreadable.append(name)
                    continue
                else:
                    plaintext = stored
                    outcome = encrypted
                token = encrypt_for_storage(plaintext)
                limit = COLUMN_LIMITS[table][column]
                if limit is not None and len(token) > limit:
                    too_long.append(name)
                    continue
                values[column] = token
                outcome.append(name)
            if values:
                writes.append((table, row.id, values))

        if unreadable:
            message = (
                f"{len(unreadable)} stored encrypted values decrypt under neither "
                f"SECURITY__FIELD_ENCRYPTION_KEY nor the other key: "
                f"{_named(unreadable)}. Nothing was changed {_GUIDE}."
            )
            logger.error("Re-encryption refused", extra={"detail": message})
            raise UnreadableEncryptedValuesException(message)

        for table, row_id, values in writes:
            await EncryptedValueCRUD.write_stored(db, table, row_id, values)
        logger.warning(
            "Re-encrypted stored values under the configured key",
            extra={
                "reencrypted": len(reencrypted),
                "encrypted": len(encrypted),
                "left_too_long": len(too_long),
                "already_readable": already,
            },
        )
        return ReencryptionReport(
            already_readable=already,
            reencrypted=reencrypted,
            encrypted=encrypted,
            left_too_long=too_long,
        )
