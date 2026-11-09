"""encrypt_plaintext_values — every encrypted column's value still stored in
the clear is encrypted under the server's key.

The encrypted column types read a value stored before encryption as it is, so
a check's target, check_config or connection string, or a maintenance job's
params, written before its column was encrypted kept its secrets in plaintext
until the row was next saved. This revision encrypts each such value with the
server's key (SECURITY__FIELD_ENCRYPTION_KEY), the stored text exactly as it is,
so it reads back as it read before: a config that read as nothing (invalid or
bare JSON) still does.

- A value the server's key decrypts is never touched, though it is stored
  line-wrapped or quoted (Fernet's decoder skips what is not base64, so the
  column reads it as its plaintext already).
- A value shaped as a token is never touched, whichever key wrote it. Before
  migrations run, alembic/env.py refuses a token the server's key cannot
  decrypt, so at this point every token is the server's.
- A value whose token would not fit its column (target and the connection
  string are varchar(1000)) is left as it is, with a warning naming it as
  ``table.column id=<row>``: saving the check with a shorter value encrypts it.
- No key while some value is plaintext: the upgrade fails, naming how many,
  rather than leave them in the clear.

Only plaintext values are written, so the revision is safe to run again.
Downgrading changes nothing: the values read the same either way.

The token shape is frozen here (as `app/core/encrypted_types.py` has it now), so
a later change to that module cannot change what this revision did.

Revision ID: 3f9c7a1b5e82
Revises: 8a4f2c6e1d37
Create Date: 2026-11-08

"""

import base64
import binascii
import logging
import re

import sqlalchemy as sa
from alembic import op
from app.core.config import settings
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.engine import Connection

revision = "3f9c7a1b5e82"
down_revision = "8a4f2c6e1d37"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_PAGE = 1000

# A Fernet token: url-safe base64 alone (with the surrounding whitespace
# Fernet's decoder ignores) of at least 57 bytes, the first 0x80.
_TOKEN_ALPHABET = re.compile(r"[A-Za-z0-9_-]+={0,2}")
_TOKEN_MIN_BYTES = 57
_TOKEN_VERSION = 0x80

# (table, its encrypted columns with the longest value each holds, None for text).
_TABLES: tuple[tuple[str, dict[str, int | None]], ...] = (
    (
        "checks",
        {"target": 1000, "check_config": None, "connection_string_encrypted": 1000},
    ),
    ("maintenance_jobs", {"params": None}),
)


def _is_token(value: str) -> bool:
    token = value.strip()
    if not _TOKEN_ALPHABET.fullmatch(token):
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode())
    except ValueError, binascii.Error:
        return False
    return len(raw) >= _TOKEN_MIN_BYTES and raw[0] == _TOKEN_VERSION


def _decrypts(fernet: Fernet, value: str) -> bool:
    try:
        fernet.decrypt(value.encode())
    except InvalidToken:
        return False
    return True


def _table(name: str, columns: dict[str, int | None]) -> sa.TableClause:
    return sa.table(
        name,
        sa.column("id", sa.Uuid()),
        *(sa.column(column, sa.Text()) for column in columns),
    )


def _rows(
    connection: Connection, table: sa.TableClause, columns: dict[str, int | None]
) -> list[sa.Row[tuple[object, ...]]]:
    """Every row of ``table`` with a plaintext value in one of ``columns``, read
    a page at a time by id."""
    found: list[sa.Row[tuple[object, ...]]] = []
    after = None
    while True:
        stmt = sa.select(table.c.id, *(table.c[c] for c in columns)).order_by(
            table.c.id
        )
        if after is not None:
            stmt = stmt.where(table.c.id > after)
        page = connection.execute(stmt.limit(_PAGE)).all()
        if not page:
            return found
        found.extend(
            row
            for row in page
            if any(isinstance(v, str) and not _is_token(v) for v in row[1:])
        )
        after = page[-1][0]


def migrate(connection: Connection) -> None:
    """Encrypt every plaintext value of every encrypted column.

    Raises:
        RuntimeError: No encryption key is configured and some value is
            plaintext; nothing is written.
    """
    found = [
        (name, columns, _table(name, columns), rows)
        for name, columns in _TABLES
        for rows in [_rows(connection, _table(name, columns), columns)]
    ]
    plaintext = sum(len(rows) for *_, rows in found)
    if not plaintext:
        return
    key = settings.security.field_encryption_key
    if not key or not key.strip():
        raise RuntimeError(
            f"{plaintext} row(s) hold encrypted columns stored in plaintext and no "
            "encryption key is configured: set SECURITY__FIELD_ENCRYPTION_KEY to "
            "the server's key, then run the migration again"
        )
    fernet = Fernet(key.encode())

    for name, columns, table, rows in found:
        for row in rows:
            row_id = row[0]
            values: dict[str, str] = {}
            stored_values: tuple[object, ...] = tuple(row[1:])
            for (column, limit), stored in zip(
                columns.items(), stored_values, strict=True
            ):
                if not isinstance(stored, str) or _is_token(stored):
                    continue
                if _decrypts(fernet, stored):
                    # A token stored wrapped or quoted: Fernet skips what is not
                    # base64, so it reads as its plaintext already.
                    continue
                token = fernet.encrypt(stored.encode()).decode()
                if limit is not None and len(token) > limit:
                    logger.warning(
                        "Left %s.%s id=%s in plaintext: encrypted, it is longer "
                        "than the column holds; save it shorter to encrypt it",
                        name,
                        column,
                        row_id,
                    )
                    continue
                values[column] = token
            if values:
                connection.execute(
                    sa.update(table).where(table.c.id == row_id).values(**values)
                )
        logger.info("Encrypted the plaintext values of %d %s row(s)", len(rows), name)


def upgrade() -> None:
    migrate(op.get_bind())


def downgrade() -> None:
    pass
