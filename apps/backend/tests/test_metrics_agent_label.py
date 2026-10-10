"""The `agent` label on every agent metric: its name, or its id while unnamed."""

from __future__ import annotations

from uuid import UUID

import pytest
from app.services.core.metrics_collector_core_service import agent_label

pytestmark = pytest.mark.unit

_ID = UUID("00000000-0000-0000-0000-0000000000aa")


def test_a_named_agent_is_labelled_by_name() -> None:
    assert agent_label("edge-01", _ID) == "edge-01"


@pytest.mark.parametrize("name", [None, ""])
def test_an_unnamed_agent_is_labelled_by_id_not_none(name: str | None) -> None:
    assert agent_label(name, _ID) == str(_ID)
