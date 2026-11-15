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
text cut out of what the agent sent, apart from a name that matches a strict
identifier pattern.
"""

import re

# The agent's own prefixes for a failure, before the exception's text
# (apps/agent/app/checks/synthetic.py).
_AGENT_PREFIXES = (
    "Script execution failed: ",
    "Script compilation failed: ",
    "Playwright initialization failed: ",
)
# The agent's message when the script outran its timeout (asyncio's TimeoutError).
_TIMED_OUT = "Script execution timed out after "
# A leading `TimeoutError:` or `ValueError:`; letters alone, so no token is read as
# a name.
_EXCEPTION_NAME = re.compile(r"([A-Z][A-Za-z]{0,63}(?:Error|Exception)):")
# Playwright's leading `Page.goto:` / `Locator.click:`: the call that failed.
_PLAYWRIGHT_CALL = re.compile(r"([A-Z][A-Za-z]{0,31}\.[a-z][A-Za-z]{0,63}):")

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


def _failed_as(error: str) -> str | None:
    """The type of what failed, read from the start of `error`, or None."""
    if error.startswith(_TIMED_OUT):
        return "TimeoutError"
    for prefix in _AGENT_PREFIXES:
        if error.startswith(prefix):
            error = error[len(prefix) :]
            break
    for pattern in (_EXCEPTION_NAME, _PLAYWRIGHT_CALL):
        match = pattern.match(error)
        if match:
            return match.group(1)
    return None


def shown_synthetic_error(error: str | None) -> str | None:
    """`error` as `Synthetic check failed: <type>` (`TimeoutError`, `Page.goto`), or
    `Synthetic check failed` when its start names no type."""
    if not error:
        return error
    name = _failed_as(error)
    return f"{FAILED}: {name}" if name else FAILED


def _figure(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _timing(value: object) -> dict[str, object]:
    """The `performance.timing` marks of `value` that are figures."""
    if not isinstance(value, dict):
        return {}
    return {
        mark: figure
        for mark, figure in value.items()
        if mark in _TIMING_MARKS and _figure(figure)
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
        if _figure(section.get(key)):
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
        names[step]: value for step, value in durations.items() if _figure(value)
    }
    shown["track_timings"] = {
        names[step]: _timing(value) for step, value in timings.items()
    }
    return shown
