"""Latency sparkline geometry for the public status cards.

The path used to be built inside the template with a namespace accumulator and
two passes of arithmetic. Templates render; the shape is computed here and
handed over finished.
"""

from typing import Any

# The <svg> the cards draw into: one x unit per bar, 20 tall, with 18 of that
# height used so the stroke is not clipped at either edge.
_HEIGHT = 20
_USABLE_HEIGHT = 18
# What a bar with no measured latency is drawn at when nothing else has one.
_FAILURE_ONLY_LATENCY = 100.0


def sparkline_path(minute_bars: list[dict[str, Any]]) -> str:
    """SVG path for a check's latency sparkline, oldest bar first.

    A bar that reported (`success` is not None) contributes a point. A failure
    with no latency — a timeout, say — is drawn at the peak rather than the
    floor, so an outage reads as a spike instead of a dip.

    Args:
        minute_bars: The check's bars, newest first (as the cards store them).

    Returns:
        The `d` attribute, or "" when no bar reported anything to plot.
    """
    bars = list(reversed(minute_bars))
    latencies = [
        bar["avg_latency"]
        for bar in bars
        if bar.get("success") is not None and bar.get("avg_latency", 0) > 0
    ]

    if latencies:
        peak, floor = max(latencies), min(latencies)
    elif any(bar.get("success") is False for bar in bars):
        # Only failures, none of them timed: draw a flat line rather than nothing.
        peak = floor = _FAILURE_ONLY_LATENCY
    else:
        return ""

    span = peak - floor or 1

    points: list[str] = []
    for x, bar in enumerate(bars):
        if bar.get("success") is None:
            continue
        latency = bar["avg_latency"] if bar.get("avg_latency", 0) > 0 else peak
        y = _HEIGHT - ((latency - floor) / span) * _USABLE_HEIGHT
        points.append(f"{'M' if not points else 'L'} {x} {y}")

    return " ".join(points)
