"""An agent's job result carries a terminal status, nothing else."""

from __future__ import annotations

import pytest
from app.models.enum_model import JobStatus
from app.schemas.job_schema import JobResultSubmit
from pydantic import ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("completed", JobStatus.COMPLETED),
        ("failed", JobStatus.FAILED),
        ("COMPLETED", JobStatus.COMPLETED),  # agents are not held to the case
    ],
)
def test_terminal_states_are_accepted(sent: str, stored: JobStatus) -> None:
    assert JobResultSubmit(status=sent).status == stored


@pytest.mark.parametrize("sent", ["running", "pending", "cancelled", "", "done"])
def test_anything_else_is_refused(sent: str) -> None:
    with pytest.raises(ValidationError):
        JobResultSubmit(status=sent)
