"""Delivery outcomes for a report POST, and the dead-letter table.

A response is SENT (2xx), PERMANENT (the LuxSwirl server itself refuses this
payload as invalid and will refuse it again), TOO_LARGE (413: the request body
is over a size limit on the way, so this payload can never pass but a smaller
one can) or TRANSIENT (anything else).

PERMANENT needs the server's own word: a 400 or 422 whose body is the app's
JSON error envelope with a payload error code (`{"error": "VALIDATION_ERROR",
...}` from request validation, `"BAD_REQUEST"` from a service ValueError). Every
other answer is TRANSIENT and the batch stays pending: network errors, 5xx,
408, 429, 401/403 (key recovery can fix those), and any 4xx without that
envelope. The trusted-host middleware answers an unlisted Host with a plain-text
`400 Invalid host header`, and a proxy such as nginx answers with its own HTML
400; those say the agent is misaddressed or the route is misconfigured, not that
the payload is bad, and dead-lettering on them would empty the pending queue
into a capped table that prunes it.

TOO_LARGE is never the payload's content: the app sends no 413, so it is a proxy
(nginx's `client_max_body_size`) refusing the body's size. The reporter sends
such a batch in halves. It never dead-letters on a 413: a result's long text
fields are capped far below any body limit, so a 413 on one result is usually
the route's fault, and the result stays pending while the rest goes.

A PERMANENT batch goes to `dead_letters`, in the same SQLite database as the
pending queue, so it never blocks the queue behind it. The table keeps the
server's (scrubbed, truncated) error detail and is capped, oldest pruned first.
"""

from __future__ import annotations

import json
import sqlite3
from enum import StrEnum

from app.shared.log_redaction import scrub_text

# The server's error detail kept per dead letter, after scrubbing.
MAX_DETAIL_CHARS = 2048

# The statuses the server answers an invalid payload with...
PERMANENT_STATUSES = frozenset({400, 422})
# ...and the error codes in its JSON envelope that say the payload is the fault.
PAYLOAD_ERROR_CODES = frozenset({"VALIDATION_ERROR", "BAD_REQUEST"})

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
    TOO_LARGE = "too_large"
    TRANSIENT = "transient"


def _is_payload_refusal(body: str) -> bool:
    """Whether `body` is the server's JSON error envelope blaming the payload."""
    try:
        envelope = json.loads(body)
    except ValueError:
        return False
    return isinstance(envelope, dict) and envelope.get("error") in PAYLOAD_ERROR_CODES


def classify_response(status_code: int, body: str) -> Delivery:
    """Classify a response from the reports endpoint.

    Args:
        status_code: The response status.
        body: The response body, as text.

    Returns:
        SENT for 2xx; PERMANENT for a 400/422 carrying the server's own JSON
        error envelope with a payload error code; TOO_LARGE for a 413;
        TRANSIENT for everything else, including a proxy's or the trusted-host
        middleware's 400.
    """
    if 200 <= status_code < 300:
        return Delivery.SENT
    if status_code == 413:
        return Delivery.TOO_LARGE
    if status_code in PERMANENT_STATUSES and _is_payload_refusal(body):
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
