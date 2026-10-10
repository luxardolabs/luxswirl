"""ErrorResponse stamps an aware UTC time, so it serializes with an offset.

Its default was `datetime.utcnow`, which is naive: the JSON carried no `Z` or
offset, and an ISO-8601 client reads it as local time.
"""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from app.schemas.base_schema import ErrorResponse

pytestmark = pytest.mark.unit


def test_the_default_timestamp_is_utc_aware() -> None:
    error = ErrorResponse(error="boom", message="it broke")

    assert error.timestamp.utcoffset() == timedelta(0)
    assert error.timestamp.tzinfo is not None
    assert error.model_dump(mode="json")["timestamp"].endswith("Z")
    assert error.timestamp.astimezone(UTC) == error.timestamp
