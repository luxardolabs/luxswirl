"""The sparkline path must keep the shape the template used to draw.

The path was built in the template with a namespace accumulator; moving it into
the view service is only safe if it emits the same geometry — including the two
choices that are easy to lose: a bar with no data contributes no point, and a
failure with no measured latency is drawn at the peak so an outage reads as a
spike rather than a dip.
"""

from __future__ import annotations

from app.core.sparkline import sparkline_path


def _bar(success, latency, minutes_ago=0):
    return {
        "success": success,
        "count": 1 if success is not None else 0,
        "avg_latency": latency,
        "http_status_code": None,
        "minutes_ago": minutes_ago,
    }


def test_no_bars_is_no_path():
    assert sparkline_path([]) == ""


def test_all_unknown_is_no_path():
    assert sparkline_path([_bar(None, 0), _bar(None, 0)]) == ""


def test_bars_render_oldest_first():
    """Bars are stored newest-first; the path runs left to right in time."""
    # newest 10ms, oldest 20ms → x=0 is the 20ms sample, which is the peak (y=2)
    path = sparkline_path([_bar(True, 10), _bar(True, 20)])
    assert path.startswith("M 0 ")
    assert " L 1 " in path
    assert path == "M 0 2.0 L 1 20.0"


def test_flat_series_does_not_divide_by_zero():
    path = sparkline_path([_bar(True, 5), _bar(True, 5)])
    assert path == "M 0 20.0 L 1 20.0"


def test_unknown_bars_are_skipped_not_plotted():
    path = sparkline_path([_bar(True, 10), _bar(None, 0), _bar(True, 20)])
    # three bars, two points — and the gap keeps its x slot
    assert path == "M 0 2.0 L 2 20.0"


def test_untimed_failure_is_drawn_at_the_peak():
    """A timeout reports success=False with no latency; it must spike, not dip."""
    # oldest→newest: 30ms, 10ms, timeout. The timeout takes the peak's y (2.0),
    # not the floor's (20.0).
    path = sparkline_path([_bar(False, 0), _bar(True, 10), _bar(True, 30)])
    assert path == "M 0 2.0 L 1 20.0 L 2 2.0"


def test_a_leading_unknown_bar_still_starts_the_path_with_M():
    """The template used loop.first — the first BAR, not the first plotted point.

    A card whose oldest bar had no data therefore emitted a path beginning with
    L and no M, which is invalid SVG: the browser drew nothing. A quarter of
    randomly generated bar series hit it.
    """
    path = sparkline_path([_bar(True, 10), _bar(True, 30), _bar(None, 0)])
    assert path.startswith("M 1 ")
    assert path == "M 1 2.0 L 2 20.0"


def test_failures_with_no_latency_at_all_still_draw_a_line():
    path = sparkline_path([_bar(False, 0), _bar(False, 0)])
    assert path == "M 0 20.0 L 1 20.0"
