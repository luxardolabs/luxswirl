"""closed_set_encrypted_configs — revision c24bfe691550's check_config fix-up,
applied to the configs it could not read.

c24bfe691550 read `checks.check_config` with whatever key alembic's process
had. A server whose key lives only in its key file had none there, so every
encrypted config read as nothing and was skipped: such a check can still hold
an `http_method` or `record_type` outside its closed set, which fails
validation on read and stops its agent fetching checks.

Every config is read with the server's key (SECURITY__FIELD_ENCRYPTION_KEY) and given
the same fix-up: `http_method` / `record_type` uppercased, a value still
outside its set cleared and the check disabled, its id and old value logged.
Only a config the fix-up changes is written, always encrypted with the key; a
config already in its sets is left byte for byte as it is, so the revision is
safe to run again.

- No key while some config is encrypted: the upgrade fails, naming how many
  and how to supply the key, rather than leave those checks unfixed. With no
  encrypted config there is nothing it needs a key for.
- A config that neither decrypts with the key nor parses as JSON (stored under
  another key) is skipped with a warning naming the check: the server cannot
  read it either.

The sets are frozen here, as in c24bfe691550, so a later enum edit cannot
change what this revision did.

Revision ID: 609ddde70f0b
Revises: e1b41fd38554
Create Date: 2026-11-02

"""

import json
import logging
from typing import Any

import sqlalchemy as sa
from alembic import op
from app.core.config import settings
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection

revision = "609ddde70f0b"
down_revision = "e1b41fd38554"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)
DNS_RECORD_TYPES = frozenset(
    {"A", "AAAA", "CNAME", "MX", "TXT", "NS", "PTR", "SOA", "SRV", "CAA"}
)
CLOSED_SETS = (("http_method", HTTP_METHODS), ("record_type", DNS_RECORD_TYPES))

# The raw column: this revision decrypts and encrypts itself, so nothing it
# writes can go through a path that stores JSON text.
checks = sa.table(
    "checks",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("check_config", sa.Text()),
    sa.column("enabled", sa.Boolean()),
)


def _normalize(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The config with its closed-set fields brought into their sets, and the
    keys whose value could not be (cleared)."""
    fixed = dict(config)
    cleared: list[str] = []
    for key, allowed in CLOSED_SETS:
        if key not in fixed or fixed[key] is None:
            continue
        value = fixed[key]
        upper = value.strip().upper() if isinstance(value, str) else None
        if upper in allowed:
            fixed[key] = upper
        elif upper == "":
            del fixed[key]  # an empty form value was always "unset"
        else:
            del fixed[key]
            cleared.append(key)
    return fixed, cleared


def _json_object(text: str) -> dict[str, Any] | None:
    """The JSON object in ``text``; None for anything else."""
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _server_fernet() -> Fernet | None:
    """The cipher for the server's key, or None when it has none."""
    key = settings.security.field_encryption_key
    return Fernet(key.encode()) if key and key.strip() else None


def migrate(connection: Connection) -> None:
    """Bring every check's config into its closed sets, encrypted.

    Raises:
        RuntimeError: No encryption key is configured and some config is
            encrypted; nothing is written.
    """
    rows = connection.execute(
        sa.select(checks.c.id, checks.c.check_config).where(
            checks.c.check_config.is_not(None)
        )
    ).all()
    fernet = _server_fernet()
    if fernet is None:
        encrypted = sum(1 for _, raw in rows if not _is_json(raw))
        if encrypted:
            raise RuntimeError(
                f"{encrypted} check config(s) are encrypted and no encryption key "
                "is configured: set SECURITY__FIELD_ENCRYPTION_KEY to the key they were "
                "encrypted with, then run the migration again"
            )
        return

    for check_id, raw in rows:
        try:
            config = _json_object(fernet.decrypt(raw.encode()).decode())
        except InvalidToken:
            if not _is_json(raw):
                logger.warning(
                    "Skipped check %s: its config does not decrypt with the "
                    "configured encryption key",
                    check_id,
                )
                continue
            config = _json_object(raw)
        if config is None:
            continue
        fixed, cleared = _normalize(config)
        if fixed == config:
            continue
        values: dict[str, Any] = {
            "check_config": fernet.encrypt(json.dumps(fixed).encode()).decode()
        }
        if cleared:
            values["enabled"] = False
            logger.warning(
                "Disabled check %s: cleared %s outside its closed set",
                check_id,
                ", ".join(f"{k}={config[k]!r}" for k in cleared),
            )
        connection.execute(
            sa.update(checks).where(checks.c.id == check_id).values(**values)
        )


def upgrade() -> None:
    migrate(op.get_bind())


def downgrade() -> None:
    """Nothing to undo: the values the upgrade replaced were invalid, and are
    not kept."""
