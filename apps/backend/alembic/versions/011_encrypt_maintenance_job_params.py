"""encrypt_maintenance_job_params — a maintenance job's params, encrypted at rest.

An import job's params hold the entries it will make: check targets, database
connection strings and synthetic scripts, the values `checks` keeps encrypted.
The column becomes text holding the Fernet ciphertext (`EncryptedJSON`), and
every row already in it is encrypted with the server's key. With no key
anywhere the server has never run, and the rows stay JSON text, which
`EncryptedJSON` reads and stores in that case.

Downgrading writes each row back as JSON. A row it cannot decrypt (no key, or
another one) stops the downgrade, naming the job: it is never written over.

Revision ID: 5b8d2f4a7c61
Revises: 3c7e1a9d5f20
Create Date: 2026-10-28

"""

import json

import sqlalchemy as sa
from alembic import op
from app.core.config import settings
from app.core.secrets import load_existing
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.engine import Connection

revision = "5b8d2f4a7c61"
down_revision = "3c7e1a9d5f20"
branch_labels = None
depends_on = None

_OLD_COMMENT = "Kind-specific args (check_ids list, URL list, etc.)"
_NEW_COMMENT = "Kind-specific args (check_ids list, URL list, etc.) - encrypted at rest"

jobs = sa.table(
    "maintenance_jobs",
    sa.column("id", sa.Uuid()),
    sa.column("params", sa.Text()),
)


def _server_fernet() -> Fernet | None:
    """The cipher for the server's key, or None when it has none.

    `alembic upgrade` runs before the server, whose lifespan resolves the key
    from its file; this reads the same env var and file, never generating one.
    """
    key = load_existing("field_encryption_key", settings.security.field_encryption_key)
    return Fernet(key.encode()) if key else None


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def encrypt_rows(connection: Connection) -> None:
    """Encrypt every row still stored as JSON text."""
    fernet = _server_fernet()
    if fernet is None:
        return
    for job_id, params in connection.execute(sa.select(jobs.c.id, jobs.c.params)).all():
        if params is not None and _is_json(params):
            connection.execute(
                sa.update(jobs)
                .where(jobs.c.id == job_id)
                .values(params=fernet.encrypt(params.encode()).decode())
            )


def decrypt_rows(connection: Connection) -> None:
    """Write every encrypted row back as the JSON it holds.

    Raises:
        RuntimeError: A row is neither JSON nor decryptable with the server's
            key; nothing is written over it.
    """
    fernet = _server_fernet()
    for job_id, params in connection.execute(sa.select(jobs.c.id, jobs.c.params)).all():
        if params is None or _is_json(params):
            continue
        if fernet is None:
            raise RuntimeError(
                f"maintenance job {job_id} holds encrypted params and no "
                "encryption key is configured; set SECURITY__FIELD_ENCRYPTION_KEY"
            )
        try:
            plain = fernet.decrypt(params.encode()).decode()
        except InvalidToken:
            raise RuntimeError(
                f"maintenance job {job_id}'s params do not decrypt with the "
                "configured encryption key"
            ) from None
        connection.execute(
            sa.update(jobs).where(jobs.c.id == job_id).values(params=plain)
        )


def upgrade() -> None:
    op.alter_column(
        "maintenance_jobs",
        "params",
        type_=sa.Text(),
        existing_type=sa.JSON(),
        existing_nullable=False,
        postgresql_using="params::text",
        comment=_NEW_COMMENT,
        existing_comment=_OLD_COMMENT,
    )
    encrypt_rows(op.get_bind())


def downgrade() -> None:
    decrypt_rows(op.get_bind())
    op.alter_column(
        "maintenance_jobs",
        "params",
        type_=sa.JSON(),
        existing_type=sa.Text(),
        existing_nullable=False,
        postgresql_using="params::json",
        comment=_OLD_COMMENT,
        existing_comment=_NEW_COMMENT,
    )
