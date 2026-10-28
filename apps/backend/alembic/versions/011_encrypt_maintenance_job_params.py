"""encrypt_maintenance_job_params — a maintenance job's params, encrypted at rest.

An import job's params hold the entries it will make: check targets, database
connection strings and synthetic scripts, the values `checks` keeps encrypted.
The column becomes text holding the Fernet ciphertext (`EncryptedJSON`), and
every row already in it is encrypted. Downgrading writes each row back as JSON.

Revision ID: 5b8d2f4a7c61
Revises: 3c7e1a9d5f20
Create Date: 2026-10-28

"""

import json

import sqlalchemy as sa
from alembic import op
from app.core.config import settings
from app.core.encrypted_types import EncryptedJSON
from app.core.secrets import load_existing
from sqlalchemy.engine import Connection

revision = "5b8d2f4a7c61"
down_revision = "3c7e1a9d5f20"
branch_labels = None
depends_on = None

_OLD_COMMENT = "Kind-specific args (check_ids list, URL list, etc.)"
_NEW_COMMENT = "Kind-specific args (check_ids list, URL list, etc.) - encrypted at rest"

encrypted_jobs = sa.table(
    "maintenance_jobs",
    sa.column("id", sa.Uuid()),
    sa.column("params", EncryptedJSON()),
)
plain_jobs = sa.table(
    "maintenance_jobs",
    sa.column("id", sa.Uuid()),
    sa.column("params", sa.Text()),
)


def _use_the_server_key() -> None:
    """Encrypt with the server's key. `alembic upgrade` runs before the server,
    whose lifespan resolves the key from its file; with no key anywhere the
    server has never run, and `EncryptedJSON` stores JSON as it would then."""
    key = load_existing("field_encryption_key", settings.security.field_encryption_key)
    if key:
        settings.security.field_encryption_key = key


def encrypt_rows(connection: Connection) -> None:
    """Write every row's params back through `EncryptedJSON`, so each is stored
    encrypted (a row it reads as plaintext JSON is the one to encrypt)."""
    _use_the_server_key()
    rows = connection.execute(
        sa.select(encrypted_jobs.c.id, encrypted_jobs.c.params)
    ).all()
    for job_id, params in rows:
        connection.execute(
            sa.update(encrypted_jobs)
            .where(encrypted_jobs.c.id == job_id)
            .values(params=params if params is not None else {})
        )


def decrypt_rows(connection: Connection) -> None:
    """Write every row's params back as plain JSON text."""
    _use_the_server_key()
    rows = connection.execute(
        sa.select(encrypted_jobs.c.id, encrypted_jobs.c.params)
    ).all()
    for job_id, params in rows:
        connection.execute(
            sa.update(plain_jobs)
            .where(plain_jobs.c.id == job_id)
            .values(params=json.dumps(params if params is not None else {}))
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
