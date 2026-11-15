"""A synthetic check's result as a viewer reads it: the error as the type of what
failed, the metrics as figures. And a result of any other check: its error as
written, credentials masked, the check's own nameserver cut to its origin."""

from __future__ import annotations

import pytest
from app.core.synthetic_result import shown_synthetic_error, shown_synthetic_metrics
from app.core.target_origin import masked_error
from app.models.enum_model import CheckType

LINK_ID = "9f8e7d6c5b4a39281706"


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (
            "Script execution timed out after 30s",
            "Synthetic check failed: TimeoutError",
        ),
        (
            "Script execution failed: TimeoutError: waiting for "
            f"https://app.example.com/reset/{LINK_ID}",
            "Synthetic check failed: TimeoutError",
        ),
        (
            "Script execution failed: Page.goto: net::ERR_NAME_NOT_RESOLVED at "
            f"https://app.example.com/reset/{LINK_ID}",
            "Synthetic check failed: Page.goto",
        ),
        (
            "Playwright initialization failed: BrowserType.launch: Executable "
            "doesn't exist",
            "Synthetic check failed: BrowserType.launch",
        ),
        ("ValueError: bad value", "Synthetic check failed: ValueError"),
        # A name with a figure in it, or past the pattern, could be a token.
        (f"Script execution failed: X{LINK_ID}Error: x", "Synthetic check failed"),
        ("Script execution failed: " + "A" * 80 + "Error: x", "Synthetic check failed"),
        (f"Script execution failed: page.goto: at /{LINK_ID}", "Synthetic check failed"),
        (
            'Script execution failed: {"next":"https:\\/\\/app.example.com\\/reset\\/'
            + LINK_ID
            + '"}',
            "Synthetic check failed",
        ),
        (f"//app.example.com/reset/{LINK_ID}", "Synthetic check failed"),
        ("Error: plain", "Synthetic check failed"),
        (None, None),
        ("", ""),
    ],
)
def test_a_synthetic_error_reads_as_the_type_of_what_failed(
    error: str | None, shown: str | None
) -> None:
    assert shown_synthetic_error(error) == shown


def test_masked_error_reads_a_synthetic_checks_error_as_its_type() -> None:
    error = f"Script execution failed: Page.goto: at https://a.example/r/{LINK_ID}"

    assert masked_error(error, (), CheckType.SYNTHETIC) == (
        "Synthetic check failed: Page.goto"
    )


@pytest.mark.parametrize(
    "error",
    [
        "expected agent 1.2.3/4 got 1.2.2",
        "Error: ENOENT C:/Users/ops/report.txt",
        "at Object.<anonymous> (node:internal/modules/cjs/loader:1105:14)",
        "control: accepted and/or refused",
    ],
)
def test_a_non_synthetic_error_with_no_credential_reads_as_written(error: str) -> None:
    assert masked_error(error, (), CheckType.HTTP) == error


def test_a_non_synthetic_error_keeps_its_credentials_masked() -> None:
    error = "connect to postgresql://monitor:pw@db.internal:5432/app failed"

    assert masked_error(error, (), CheckType.POSTGRES) == (
        "connect to postgresql://***@db.internal:5432/app failed"
    )


def test_the_synthetic_metrics_keep_figures_and_no_text() -> None:
    first, second = f"https://a.example/x/{LINK_ID}", f"https://a.example/y/{LINK_ID}"

    shown = shown_synthetic_metrics(
        {
            "status": "passed",
            "errors": [first],
            "console_errors": [first],
            "request_failures": [{"url": first, "failure": first}],
            "steps": [first],
            "browser_timing": {"navigationStart": 1, "loadEventEnd": 9, first: 3},
            "script_execution_time_ms": 12.5,
            "total_execution_time_ms": True,
            "track_durations": {first: 1.0, second: "x"},
            "track_timings": {second: {"domComplete": 4, "name": first}, first: {}},
            "custom": first,
        }
    )

    assert shown == {
        "status": "failure",
        "script_execution_time_ms": 12.5,
        "browser_timing": {"navigationStart": 1, "loadEventEnd": 9},
        "track_durations": {"Step 1": 1.0},
        "track_timings": {"Step 2": {"domComplete": 4}, "Step 1": {}},
    }


@pytest.mark.parametrize(
    ("status", "shown"),
    [("success", "success"), ("unknown", "unknown"), (None, "unknown"), (7, "failure")],
)
def test_the_synthetic_status_reads_as_one_of_three(status: object, shown: str) -> None:
    assert shown_synthetic_metrics({"status": status})["status"] == shown


def test_a_synthetic_section_that_is_no_mapping_reads_as_unknown() -> None:
    assert shown_synthetic_metrics([LINK_ID]) == {"status": "unknown"}
