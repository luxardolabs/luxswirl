"""Capture the app's own log records, with their `extra` fields intact.

`caplog` does not work on this app's loggers. `shared.logger.setup_logging()`
sets `propagate = False` on the `luxswirl` namespace root (deliberately — it
keeps our output isolated from whatever else configured the global root), and
caplog attaches its handler to the GLOBAL root. So a caplog assertion passes
when the test file runs alone, before anything has called `setup_logging()`, and
silently captures nothing once the full suite has — which is the worst shape for
a test to have.

`tests/checks/test_ssl_expiry.py` works around it by monkeypatching the module's
`logger.warning`, which is fine for a message but throws the `extra` away. An
audit record IS its extra fields, so this fixture attaches a real handler to the
namespace root instead and hands back the records.
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
