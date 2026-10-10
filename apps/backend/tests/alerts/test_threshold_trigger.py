"""The threshold trigger compares numbers, whatever JSON type the threshold came in.

`trigger_config` is a free-form JSON object on the API, so a threshold can
arrive as `"1000"`. The evaluator `cast` it to float, which converts nothing:
comparing the latency to the string raised TypeError mid-evaluation.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from app.models.alert_model import Alert
from app.models.check_result_model import CheckResult
from app.services.core.alert_core_service import AlertCoreService

pytestmark = pytest.mark.unit


async def _fires(latency_ms: float | None, config: dict[str, Any]) -> bool:
    result = CheckResult(latency_ms=latency_ms)
    # The threshold trigger reads only the alert's id, the result and its config.
    return await AlertCoreService._evaluate_threshold(
        None, Alert(id=uuid4()), None, result, config
    )


@pytest.mark.parametrize("value", [1000, 1000.0, "1000", "1000.0"])
async def test_a_numeric_or_numeric_string_threshold_compares(value: Any) -> None:
    assert await _fires(1500.0, {"operator": ">", "value": value})
    assert not await _fires(500.0, {"operator": ">", "value": value})


@pytest.mark.parametrize("value", ["fast", [1000], {"ms": 1}])
async def test_a_threshold_that_is_not_a_number_never_fires(value: Any) -> None:
    assert not await _fires(1500.0, {"operator": ">", "value": value})
