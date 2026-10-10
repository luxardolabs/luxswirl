"""Capture the app's own log records, with their `extra` fields intact.

The handler sits on the `luxswirl` namespace logger itself, so it sees every
`luxswirl.*` record whatever the root logger's handlers and level are (the
fleet logging module replaces the root handler each time `configure_logging()`
runs, which the app and alembic's env.py both do). The namespace level is
dropped to DEBUG for the test so an INFO record is not filtered first.

Records carry their `extra` fields as attributes (a colliding key is renamed
`extra_<key>` by the module), and an audit record IS its extra fields.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

_NAMESPACE = "luxswirl"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def app_logs() -> Iterator[list[logging.LogRecord]]:
    """Every record the `luxswirl.*` loggers emit during the test.

    Returns the live list, so assert after the call under test. Records carry
    their `extra` fields as attributes, which is the point.
    """
    root = logging.getLogger(_NAMESPACE)
    handler = _Capture()
    previous_level = root.level
    root.addHandler(handler)
    # The namespace root filters by level, so a WARNING-configured root would
    # drop the INFO records a test wants to see.
    root.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
