"""A synthetic check's result as a viewer reads it: the error as the type of what
failed, the metrics as figures. And a result of any other check: its error as
written, credentials masked, the check's own target and nameserver cut to their
origin. Whether a result is a synthetic run's is decided per result."""

from __future__ import annotations

import json

import pytest
from app.core.synthetic_result import shown_synthetic_error, shown_synthetic_metrics
from app.core.target_origin import is_synthetic_result, masked_error

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
        (
            f"Script execution failed: page.goto: at /{LINK_ID}",
            "Synthetic check failed",
        ),
        (
            'Script execution failed: {"next":"https:\\/\\/app.example.com\\/reset\\/'
            + LINK_ID
            + '"}',
            "Synthetic check failed",
        ),
        (f"//app.example.com/reset/{LINK_ID}", "Synthetic check failed"),
        ("Error: plain", "Synthetic check failed: Error"),
        # The agent writes the exception's class first, then Playwright's call.
        (
            "Script execution failed: TimeoutError: Page.goto: Timeout 30000ms "
            f"exceeded at https://app.example.com/reset/{LINK_ID}",
            "Synthetic check failed: Page.goto",
        ),
        (
            "Script execution failed: Error: Locator.click: Target closed",
            "Synthetic check failed: Locator.click",
        ),
        (
            "Script execution failed: Page.waitForSelector: x",
            "Synthetic check failed: Page.waitForSelector",
        ),
        # A word the script or the page chose is no name, whatever its shape.
        (
            "CorrectHorseBatteryStapleError: login refused; trace",
            "Synthetic check failed",
        ),
        (
            "Script execution failed: Intranet.corpsecretname: x",
            "Synthetic check failed",
        ),
        ("Script execution failed: Page.corpsecretname: x", "Synthetic check failed"),
        ("Script execution failed: bad", "Synthetic check failed"),
        (None, None),
        ("", ""),
    ],
)
def test_a_synthetic_error_reads_as_the_type_of_what_failed(
    error: str | None, shown: str | None
) -> None:
    assert shown_synthetic_error(error) == shown


@pytest.mark.parametrize(
    ("reported", "shown"),
    [
        ("ValueError", "Synthetic check failed: ValueError"),
        ("TargetClosedError", "Synthetic check failed: TargetClosedError"),
        # A name outside the vocabulary, or no name, is not shown.
        ("CorrectHorseBatteryStapleError", "Synthetic check failed"),
        (f"X{LINK_ID}", "Synthetic check failed"),
        (7, "Synthetic check failed"),
        (None, "Synthetic check failed"),
    ],
)
def test_the_exception_class_the_agent_reports_is_shown_from_the_vocabulary(
    reported: object, shown: str
) -> None:
    error = f"Script execution failed: bad value at /reset/{LINK_ID}"

    assert shown_synthetic_error(error, {"error_type": reported}) == shown


def test_masked_error_reads_a_synthetic_checks_error_as_its_type() -> None:
    error = f"Script execution failed: Page.goto: at https://a.example/r/{LINK_ID}"

    assert masked_error(error, None, ran_synthetic=True, configured=()) == (
        "Synthetic check failed: Page.goto"
    )


@pytest.mark.parametrize(
    "metrics",
    [
        {"synthetic": {"status": "failure", "error_type": "ValueError"}},
        json.dumps({"synthetic": {"status": "failure", "error_type": "ValueError"}}),
    ],
)
def test_a_run_whose_metrics_carry_a_synthetic_section_is_a_synthetic_run(
    metrics: object,
) -> None:
    """Whatever its check is now: a check retyped away from synthetic keeps its
    old runs, and an agent not yet reloaded keeps running the old script."""
    error = f"Script execution failed: ValueError: bad at /reset/{LINK_ID}"

    assert is_synthetic_result(metrics, ran_synthetic=False)
    assert masked_error(error, metrics, ran_synthetic=False, configured=()) == (
        "Synthetic check failed: ValueError"
    )


def test_every_run_of_a_check_that_ever_ran_a_script_is_a_synthetic_run() -> None:
    """The agent cuts an oversized result's metrics to a size note, so the run's
    own metrics cannot be relied on to say it was a script's."""
    error = f"Script execution failed: refused at https://a.example/r/{LINK_ID}"

    assert is_synthetic_result({"note": "metrics too large"}, ran_synthetic=True)
    assert masked_error(error, "{}", ran_synthetic=True, configured=()) == (
        "Synthetic check failed"
    )
    assert not is_synthetic_result({"response": {}}, ran_synthetic=False)
    assert not is_synthetic_result("not json", ran_synthetic=False)


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
    assert masked_error(error, None, ran_synthetic=False, configured=()) == error


def test_a_non_synthetic_error_keeps_its_credentials_masked() -> None:
    error = "connect to postgresql://monitor:pw@db.internal:5432/app failed"

    assert masked_error(error, None, ran_synthetic=False, configured=()) == (
        "connect to postgresql://***@db.internal:5432/app failed"
    )


@pytest.mark.parametrize(
    "target",
    [
        f"https://app.example.com/hooks/{LINK_ID}",
        f"host=db.internal port=5432 sslkey=/{LINK_ID}",
    ],
)
def test_a_non_synthetic_error_cuts_the_checks_own_target_to_its_origin(
    target: str,
) -> None:
    """Exactly as configured, as the nameserver is: a token in a target's path
    is one no credential pattern names."""
    error = f"connection refused: {target} (attempt 3)"

    shown = masked_error(error, None, ran_synthetic=False, configured=(target, None))

    assert LINK_ID not in shown
    assert shown.startswith("connection refused: ")
    assert shown.endswith(" (attempt 3)")


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
