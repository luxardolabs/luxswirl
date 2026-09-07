"""Display shaping for a synthetic check's browser-timing panel.

The agent reports raw `performance.timing` marks and per-step wall-clock
durations in seconds. The panel used to subtract the marks and multiply the
seconds inline, and to look each step's duration up out of a second dict by a
loop variable.
"""

from typing import Any


def build_synthetic_view(synthetic: dict[str, Any] | None) -> dict[str, Any]:
    """Millisecond figures for the synthetic panel.

    Args:
        synthetic: The `synthetic` block of a check result's metrics, if present.

    Returns:
        `synthetic_total_ms` (the whole run's navigation time) and
        `synthetic_steps`, one row per step with its timing marks already
        turned into milliseconds relative to that step's navigationStart.
    """
    if not synthetic:
        return {"synthetic_total_ms": 0, "synthetic_steps": []}

    durations: dict[str, float] = synthetic.get("track_durations") or {}
    timings: dict[str, dict[str, Any]] = synthetic.get("track_timings") or {}

    steps = []
    for name, timing in timings.items():
        start = timing.get("navigationStart") or 0

        def since_start(mark: str, start: float = start) -> int | None:
            """The mark as whole ms after navigation started, or None if absent."""
            value = timing.get(mark)
            return int(value - start) if value else None

        steps.append(
            {
                "name": name,
                "dom_load_ms": since_start("domLoading"),
                "dom_complete_ms": since_start("domComplete"),
                "full_load_ms": since_start("loadEventEnd"),
                # The agent reports the step's own duration in seconds.
                "total_ms": int(durations.get(name, 0) * 1000),
            }
        )

    return {
        "synthetic_total_ms": int(sum(durations.values()) * 1000),
        "synthetic_steps": steps,
    }
