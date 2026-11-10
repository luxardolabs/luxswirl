"""Shared fixtures for the agent's tests."""

import time
from collections.abc import Iterator

import pytest
from app.checks import _lookups


@pytest.fixture(autouse=True)
def lookups_drained() -> Iterator[None]:
    """Each test starts with no check lookup in flight.

    A lookup running for a host is shared by whoever asks for it, from any event
    loop, so a slow one an earlier test left running would answer this test's
    lookup of the same host.
    """
    deadline = time.monotonic() + 30
    while _lookups._in_flight and time.monotonic() < deadline:
        time.sleep(0.05)
    yield
