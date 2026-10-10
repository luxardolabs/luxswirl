"""`UUIDBaseModel.__repr__` — the repr every UUID model without its own inherits.

It walked `__mapper__.columns`, which yields `Column` objects, and handed each one
to `getattr` as the attribute name: `repr()` of an Agent raised `TypeError`
instead of describing it, in every log line, debugger and assertion message that
printed one.
"""

from __future__ import annotations

import re

import pytest
from fixtures.factories import make_agent

pytestmark = pytest.mark.unit


def test_repr_names_the_model_and_its_first_set_columns() -> None:
    agent = make_agent(agent_name="edge-01")

    text = repr(agent)

    assert text.startswith(f"<Agent(id={agent.id}, ")
    assert "agent_name='edge-01'" in text


def test_repr_never_repeats_the_id_and_stops_at_three_attributes() -> None:
    agent = make_agent(agent_name="edge-01")
    prefix = f"<Agent(id={agent.id}, "

    body = repr(agent).removeprefix(prefix)
    # Only the model's own column keys: a nested value (a datetime's `tzinfo=`) is not one.
    shown = [
        k
        for k in (p.key for p in agent.__mapper__.column_attrs)
        if re.search(rf"(?:^|, ){k}=", body)
    ]

    assert "id" not in shown
    assert 1 <= len(shown) <= 3


def test_repr_truncates_a_long_string() -> None:
    text = repr(make_agent(agent_name="x" * 80))

    assert f"agent_name='{'x' * 47}...'" in text
