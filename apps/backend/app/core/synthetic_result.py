"""A synthetic check's result as a reader who does not hold its script reads it.

A synthetic script drives a browser through pages its author chose, and what it
reports is free text about them: Playwright quotes the URL it was opening, a page's
console quotes what it logged, a step is named for its URL. A URL's path can be the
credential (a password-reset link, a signed download), written in any form free text
takes (JSON-escaped, scheme-relative, with a quote in it), so no pattern over that
text can find every one. A reader who is not shown the script (a viewer) is shown
none of it: the error only as the type of what failed, and of the metrics only the
figures and the status the panel draws.

An allow-list, not a deny-list: what is shown is what this module builds, never
text cut out of what the agent sent, apart from a name in the fixed vocabulary of
`playwright_names`.
"""

import math
import re
from typing import TypeGuard

from app.core.playwright_names import (
    EXCEPTION_NAMES,
    PLAYWRIGHT_CLASSES,
    PLAYWRIGHT_METHODS,
)

# The agent's own prefixes for a failure, before the exception's text
# (apps/agent/app/checks/synthetic.py).
_AGENT_PREFIXES = (
    "Script execution failed: ",
    "Script compilation failed: ",
    "Playwright initialization failed: ",
)
# The agent's message when the script outran its timeout (asyncio's TimeoutError).
_TIMED_OUT = "Script execution timed out after "
# A leading `TimeoutError: ` (the agent writes the exception's class first), then
# Playwright's leading `Page.goto: `: candidates, shown only when in the vocabulary.
_EXCEPTION_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]{0,63}): ")
_PLAYWRIGHT_CALL = re.compile(r"([A-Za-z]{1,32})\.([A-Za-z_]{1,64}):")

FAILED = "Synthetic check failed"

# The marks of the browser's `performance.timing`: the only keys of a timing read.
_TIMING_MARKS = frozenset(
    {
        "navigationStart",
        "unloadEventStart",
        "unloadEventEnd",
        "redirectStart",
        "redirectEnd",
        "fetchStart",
        "domainLookupStart",
        "domainLookupEnd",
        "connectStart",
        "connectEnd",
        "secureConnectionStart",
        "requestStart",
        "responseStart",
        "responseEnd",
        "domLoading",
        "domInteractive",
        "domContentLoadedEventStart",
        "domContentLoadedEventEnd",
        "domComplete",
        "loadEventStart",
        "loadEventEnd",
    }
)
# The run's own durations, in milliseconds.
_DURATIONS = (
    "script_execution_time_ms",
    "total_execution_time_ms",
    "browser_load_time_ms",
)


def _failed_as(error: str, section: object) -> str | None:
    """The type of what failed: the Playwright call `error` starts with, else the
    exception class the agent reported (`error_type` in the synthetic `section`,
    else first in `error`); None when neither is in the vocabulary."""
    if error.startswith(_TIMED_OUT):
        return "TimeoutError"
    for prefix in _AGENT_PREFIXES:
        if error.startswith(prefix):
            error = error[len(prefix) :]
            break
    named: str | None = None
    match = _EXCEPTION_NAME.match(error)
    if match and match.group(1) in EXCEPTION_NAMES:
        named = match.group(1)
        error = error[match.end() :]
    call = _PLAYWRIGHT_CALL.match(error)
    if (
        call
        and call.group(1) in PLAYWRIGHT_CLASSES
        and call.group(2) in PLAYWRIGHT_METHODS
    ):
        return f"{call.group(1)}.{call.group(2)}"
    reported = section.get("error_type") if isinstance(section, dict) else None
    if isinstance(reported, str) and reported in EXCEPTION_NAMES:
        return reported
    return named


def shown_synthetic_error(error: str | None, section: object = None) -> str | None:
    """`error` as `Synthetic check failed: <type>` (`Page.goto`, `TimeoutError`),
    or `Synthetic check failed` when it names no type in the vocabulary.
    `section` is the result's `synthetic` metrics, where the agent reports the
    exception's class as `error_type`."""
    if not error:
        return error
    name = _failed_as(error, section)
    return f"{FAILED}: {name}" if name else FAILED


def is_figure(value: object) -> TypeGuard[int | float]:
    """A number that is a figure: no bool, no NaN or infinity."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _timing(value: object) -> dict[str, object]:
    """The `performance.timing` marks of `value` that are figures."""
    if not isinstance(value, dict):
        return {}
    return {
        mark: figure
        for mark, figure in value.items()
        if mark in _TIMING_MARKS and is_figure(figure)
    }


def shown_synthetic_metrics(section: object) -> dict[str, object]:
    """A synthetic result's `synthetic` metrics as figures alone: the status as
    `success`, `failure` or `unknown`, the run's durations, the browser's timing
    marks, and each step's duration and marks under `Step 1`, `Step 2`, ... in the
    order the steps were reported. Every text (errors, console errors, failed
    requests, step names and descriptions, the script's own fields) is left out."""
    if not isinstance(section, dict):
        return {"status": "unknown"}
    status = section.get("status")
    shown: dict[str, object] = {
        "status": (
            "unknown"
            if status in (None, "unknown")
            else "success"
            if status == "success"
            else "failure"
        )
    }
    for key in _DURATIONS:
        if is_figure(section.get(key)):
            shown[key] = section[key]
    shown["browser_timing"] = _timing(section.get("browser_timing"))
    durations = section.get("track_durations")
    timings = section.get("track_timings")
    durations = durations if isinstance(durations, dict) else {}
    timings = timings if isinstance(timings, dict) else {}
    names: dict[object, str] = {}
    for step in [*durations, *timings]:
        names.setdefault(step, f"Step {len(names) + 1}")
    shown["track_durations"] = {
        names[step]: value for step, value in durations.items() if is_figure(value)
    }
    shown["track_timings"] = {
        names[step]: _timing(value) for step, value in timings.items()
    }
    return shown
