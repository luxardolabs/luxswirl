"""Display shaping for a synthetic check's browser-timing panel.

The agent reports raw `performance.timing` marks and per-step wall-clock
durations in seconds. Every figure the panel shows is computed here, so the
template only renders: a mark or duration the agent sent as anything but a
figure (`is_figure`: null, text, NaN, a list, a number past 2**53) is shown as no
figure, never a failed render.
"""

from typing import Any

from app.core.synthetic_result import is_figure

# The single-page tiles: each a mark of `browser_timing` after navigationStart.
_PAGE_TILES = (
    ("DOM Load (ms)", "domLoading"),
    ("DOM Complete (ms)", "domComplete"),
    ("Full Load (ms)", "loadEventEnd"),
)


def _whole(value: object) -> int | None:
    """`value` as whole milliseconds, or None when it is no figure."""
    return int(value) if is_figure(value) else None


def _since_start(timing: object, mark: str) -> int | None:
    """One timing mark as whole ms after that timing's navigationStart, or None
    when the mark is absent or zero, or either is no figure: a mark is an epoch
    time, so one counted from no start would read as decades."""
    if not isinstance(timing, dict):
        return None
    value = timing.get(mark)
    start = timing.get("navigationStart")
    if not value or not is_figure(value) or not is_figure(start):
        return None
    # Two figures, so the difference is finite; one past the limit is no figure.
    return _whole(float(value) - float(start))


def _seconds_as_ms(seconds: object) -> int:
    """A step's duration in seconds as whole ms; 0 when it is no figure."""
    if not is_figure(seconds):
        return 0
    return _whole(float(seconds) * 1000) or 0


def _page_tiles(browser_timing: object) -> list[dict[str, object]]:
    """The single-page tiles: each one's label, mark and milliseconds (or None)."""
    return [
        {"label": label, "mark": mark, "ms": _since_start(browser_timing, mark)}
        for label, mark in _PAGE_TILES
    ]


def build_synthetic_view(synthetic: object) -> dict[str, Any]:
    """Millisecond figures for the synthetic panel.

    Args:
        synthetic: The `synthetic` block of a check result's metrics, if present.

    Returns:
        `synthetic_total_ms` (the whole run's navigation time),
        `synthetic_steps`, one row per step with its timing marks already
        turned into milliseconds relative to that step's navigationStart, and
        `synthetic_page`, a tile per page mark (`browser_timing`) the same way,
        `synthetic_execution_ms`, the whole check's run (None when absent), and
        `synthetic_step_count`, the steps the run reported a duration for.
    """
    if not isinstance(synthetic, dict) or not synthetic:
        return {
            "synthetic_total_ms": 0,
            "synthetic_steps": [],
            "synthetic_page": _page_tiles(None),
            "synthetic_execution_ms": None,
            "synthetic_step_count": 0,
        }

    durations = synthetic.get("track_durations")
    timings = synthetic.get("track_timings")
    durations = durations if isinstance(durations, dict) else {}
    timings = timings if isinstance(timings, dict) else {}

    steps = [
        {
            "name": name,
            "dom_load_ms": _since_start(timing, "domLoading"),
            "dom_complete_ms": _since_start(timing, "domComplete"),
            "full_load_ms": _since_start(timing, "loadEventEnd"),
            # The agent reports the step's own duration in seconds.
            "total_ms": _seconds_as_ms(durations.get(name)),
        }
        for name, timing in timings.items()
    ]
    execution = synthetic.get("total_execution_time_ms")

    return {
        "synthetic_total_ms": sum(map(_seconds_as_ms, durations.values())),
        "synthetic_steps": steps,
        "synthetic_page": _page_tiles(synthetic.get("browser_timing")),
        "synthetic_execution_ms": _whole(execution),
        "synthetic_step_count": len(durations),
    }
