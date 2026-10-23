"""Delivery outcomes for a report POST, and the dead-letter table.

A response is SENT (2xx), PERMANENT (the server refuses this payload and will
refuse it again: 400, 413, 422) or TRANSIENT (anything else, including network
errors, 5xx, 408, 429 and 401/403; an auth failure is something the agent's key
recovery can fix, so it is never dead-lettered).

A PERMANENT batch goes to `dead_letters`, in the same SQLite database as the
pending queue, so it never blocks the queue behind it. The table keeps the
server's (scrubbed, truncated) error detail and is capped, oldest pruned first.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from shared.log_redaction import scrub_text

# The server's error detail kept per dead letter, after scrubbing.
MAX_DETAIL_CHARS = 2048

PERMANENT_STATUSES = frozenset({400, 413, 422})

CREATE_DEAD_LETTERS = """
CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    failed_at REAL NOT NULL,
    status_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    batch_size INTEGER NOT NULL,
    payload BLOB NOT NULL
)
"""


class Delivery(StrEnum):
    """What a report POST's outcome means for the batch."""

    SENT = "sent"
    PERMANENT = "permanent"
    TRANSIENT = "transient"


def classify_status(status_code: int) -> Delivery:
    """Classify an HTTP status from the reports endpoint.

    Args:
        status_code: The response status.

    Returns:
        SENT for 2xx, PERMANENT for a refusal the server will repeat
        (400, 413, 422), TRANSIENT for everything else.
    """
    if 200 <= status_code < 300:
        return Delivery.SENT
    if status_code in PERMANENT_STATUSES:
        return Delivery.PERMANENT
    return Delivery.TRANSIENT


def error_detail(body: str) -> str:
    """The server's error body as stored and logged: scrubbed, then truncated.

    Scrubbed first, so the cut can never leave half a credential unmasked.
    """
    scrubbed: str = scrub_text(body)
    return scrubbed[:MAX_DETAIL_CHARS]


def insert_dead_letter(
    conn: sqlite3.Connection,
    *,
    created_at: float,
    failed_at: float,
    status_code: int,
    detail: str,
    batch_size: int,
    payload: bytes,
) -> None:
    """Insert one dead letter (the caller commits)."""
    conn.execute(
        "INSERT INTO dead_letters "
        "(created_at, failed_at, status_code, response_body, batch_size, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (created_at, failed_at, status_code, detail, batch_size, payload),
    )


def prune_dead_letters(conn: sqlite3.Connection, cap: int) -> int:
    """Delete the oldest dead letters beyond `cap` (the caller commits).

    Returns:
        The number of rows pruned.
    """
    total = int(conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0])
    excess = total - cap
    if excess <= 0:
        return 0
    conn.execute(
        "DELETE FROM dead_letters WHERE id IN "
        "(SELECT id FROM dead_letters ORDER BY id ASC LIMIT ?)",
        (excess,),
    )
    return excess


def count_dead_letters(conn: sqlite3.Connection) -> int:
    """How many dead letters the table holds."""
    return int(conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0])
