"""An agent whose report the server answers `NOT_FOUND` (its agent row deleted
while the report was in flight) keeps the batch for replay: only a validation
refusal is permanent."""

from __future__ import annotations

import json

import pytest
from app.agent.dead_letter import Delivery, classify_response

pytestmark = pytest.mark.unit

NOT_FOUND = json.dumps(
    {
        "error": "NOT_FOUND",
        "message": "Agent not found: 0190",
        "details": {"agent_id": "0190"},
    }
)


def test_agent_not_found_404_is_transient_kept_for_replay() -> None:
    assert classify_response(404, NOT_FOUND) is Delivery.TRANSIENT


def test_a_validation_422_is_permanent() -> None:
    body = json.dumps({"error": "VALIDATION_ERROR", "message": "bad"})
    assert classify_response(422, body) is Delivery.PERMANENT
