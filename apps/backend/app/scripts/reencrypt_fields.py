"""Re-encrypt under SECURITY__FIELD_ENCRYPTION_KEY the stored values another key wrote.

When the server once ran under the wrong key, both the server and
`alembic upgrade` refuse to start, naming each value the configured key cannot
read. Give this command the other key (the one those values were written
under) on standard input, never as an argument, which `ps` and the shell
history would show:

    docker compose run --rm --no-deps -T luxswirl_server \\
        python -m app.scripts.reencrypt_fields < other-key.txt

It runs without the server (the server is what refuses to start), judges every
stored encrypted value first, and writes only when each one decrypts under the
configured key or the other one: those the other key wrote are re-encrypted
under the configured key, and values still stored in plaintext are encrypted.
A value neither key reads refuses the run, with nothing changed. Exit status 0
when it ran, 1 when it refused.
"""

import asyncio
import getpass
import sys
from typing import TextIO

from app.core.exceptions import (
    UnreadableEncryptedValuesException,
    ValidationException,
)
from app.db.database import worker_session
from app.schemas.field_encryption_schema import ReencryptionReport
from app.services.core.field_encryption_core_service import FieldEncryptionCoreService


def _read_key(stream: TextIO) -> str:
    """The other key: prompted for without echo on a terminal, else the first line."""
    if stream.isatty():
        return getpass.getpass("The other field-encryption key: ")
    return stream.readline().strip()


def _summary(report: ReencryptionReport) -> list[str]:
    lines = [
        f"Re-encrypted {len(report.reencrypted)} value(s) the other key wrote.",
        f"Encrypted {len(report.encrypted)} value(s) stored in plaintext.",
        f"Left {report.already_readable} value(s) the configured key already reads.",
    ]
    lines += [f"  re-encrypted {name}" for name in report.reencrypted]
    if report.left_too_long:
        lines.append(
            f"Left {len(report.left_too_long)} plaintext value(s) whose token "
            "would not fit the column; save each check shorter to encrypt it:"
        )
        lines += [f"  {name}" for name in report.left_too_long]
    return lines


async def reencrypt(other_key: str) -> int:
    """Run the repair with ``other_key``: 0 when it ran, 1 when it refused."""
    try:
        async with worker_session() as db:
            report = await FieldEncryptionCoreService.reencrypt_from(db, other_key)
    except (ValidationException, UnreadableEncryptedValuesException) as refused:
        print(f"Refused: {refused.message}", file=sys.stderr)
        return 1
    # Printed only after the transaction committed.
    print("\n".join(_summary(report)))
    return 0


def main(stdin: TextIO | None = None) -> int:
    other_key = _read_key(stdin if stdin is not None else sys.stdin)
    if not other_key:
        print(
            "Refused: no key on standard input (pipe the other key's file in)",
            file=sys.stderr,
        )
        return 1
    return asyncio.run(reencrypt(other_key))


if __name__ == "__main__":
    raise SystemExit(main())
