"""
Architectural lint: web UI JS must not use raw network calls.

Per LUXSWIRL-103, the LuxSwirl web UI is HTMX-first. JS modules in
`app/web/static/js/` may not introduce new `fetch(` or
`window.location.reload(` calls — server roundtrips go through HTMX
(`hx-post`/`hx-get`/`hx-delete`/etc. or `htmx.ajax(...)` for cases that
need dynamic values), and refresh-page UX comes from the server-fired
`refreshPage` HX-Trigger event.

Two narrow allowlist entries are documented and intentional:

- `app.js` — the canonical `refreshPage` event handler that calls
  `window.location.reload()`. Server endpoints fire this event via
  `HX-Trigger: refreshPage` to request a full reload; the listener is
  the architecture, not a violation.
- `database-health.js` — Chart.js data refresh via `fetch()` for the
  hours-selector. Pure client-side chart rendering with a JSON-data
  refresh is the standard Chart.js pattern; HTMX swap of a `<script>`
  tag with chart-update code is awkward and not idiomatic. Section B
  of the original audit explicitly carved out Chart.js rendering.

Any new violation should be either converted to HTMX or, if genuinely
unavoidable, added to the allowlist below with a comment justifying it.
"""

from __future__ import annotations

import re
from pathlib import Path

from _paths import BACKEND_ROOT

JS_DIR = BACKEND_ROOT / "web" / "static" / "js"

# Vendored libraries (Three.js, etc.) are excluded from the lint.
VENDOR_DIRS = {"vendor"}

# Files allowed to use the listed pattern, with rationale. Keys are
# (filename, pattern); values are short justifications referenced when
# a violation is reported.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "app.js",
        "window.location.reload(",
    ): "canonical refreshPage HX-Trigger event handler",
    (
        "database-health.js",
        "fetch(",
    ): "Chart.js data refresh — Section B carve-out for chart rendering",
}

FETCH_PATTERN = re.compile(r"\bfetch\s*\(")
RELOAD_PATTERN = re.compile(r"\bwindow\.location\.reload\s*\(")


def _js_files() -> list[Path]:
    """All same-origin JS modules under web/static/js (excluding vendor)."""
    out: list[Path] = []
    for path in JS_DIR.rglob("*.js"):
        if any(part in VENDOR_DIRS for part in path.relative_to(JS_DIR).parts):
            continue
        out.append(path)
    return out


def test_no_unauthorized_fetch_calls() -> None:
    """No `fetch(` outside the documented allowlist."""
    violations: list[str] = []
    for path in _js_files():
        text = path.read_text(encoding="utf-8")
        if not FETCH_PATTERN.search(text):
            continue
        rationale = ALLOWLIST.get((path.name, "fetch("))
        if rationale is None:
            for lineno, line in enumerate(text.splitlines(), start=1):
                if FETCH_PATTERN.search(line):
                    violations.append(
                        f"app/{path.relative_to(BACKEND_ROOT)}:{lineno}: {line.strip()}"
                    )
    assert not violations, (
        "Web UI JS must not use raw fetch() — use HTMX (hx-post/hx-get/"
        "htmx.ajax) instead. To add a justified exception, edit the ALLOWLIST "
        "in tests/test_no_raw_js_network.py with a written rationale.\n\n"
        "Violations:\n  " + "\n  ".join(violations)
    )


def test_no_unauthorized_reload_calls() -> None:
    """No `window.location.reload(` outside the documented allowlist."""
    violations: list[str] = []
    for path in _js_files():
        text = path.read_text(encoding="utf-8")
        if not RELOAD_PATTERN.search(text):
            continue
        rationale = ALLOWLIST.get((path.name, "window.location.reload("))
        if rationale is None:
            for lineno, line in enumerate(text.splitlines(), start=1):
                if RELOAD_PATTERN.search(line):
                    violations.append(
                        f"app/{path.relative_to(BACKEND_ROOT)}:{lineno}: {line.strip()}"
                    )
    assert not violations, (
        "Web UI JS must not call window.location.reload() — fire the "
        "refreshPage HX-Trigger event from the server instead. The single "
        "legitimate caller is app.js's refreshPage event listener. To add a "
        "justified exception, edit the ALLOWLIST in "
        "tests/test_no_raw_js_network.py with a written rationale.\n\n"
        "Violations:\n  " + "\n  ".join(violations)
    )


def test_no_unauthorized_alert_calls() -> None:
    """No `alert(` in web UI JS — use window.showToast(message, kind) instead."""
    alert_pattern = re.compile(r"\balert\s*\(")
    violations: list[str] = []
    for path in _js_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Skip comments
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if alert_pattern.search(line):
                violations.append(f"app/{path.relative_to(BACKEND_ROOT)}:{lineno}: {stripped}")
    assert not violations, (
        "Web UI JS must not use alert() — use window.showToast(message, kind) "
        "for user-facing messages.\n\nViolations:\n  " + "\n  ".join(violations)
    )
