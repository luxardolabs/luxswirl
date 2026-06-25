"""
Shared HTMX response helpers for web routers.

Underscore-prefixed module — exempt from the `*_router.py` naming rule
because it's a multi-consumer helper (used by every web router that needs
to emit `HX-Trigger` events alongside an empty or partial body).
"""

import json
from typing import Any

from fastapi.responses import HTMLResponse


def hx_trigger(events: dict[str, Any]) -> str:
    """
    Build an HX-Trigger header value from a dict of event-name → payload.

    Example:
        headers={"HX-Trigger": hx_trigger({"showToast": {"message": "Saved", "type": "success"}})}
    """
    return json.dumps(events)


def hx_toast_trigger(message: str, kind: str = "success") -> str:
    """Build an HX-Trigger header value that fires the showToast event."""
    return hx_trigger({"showToast": {"message": message, "type": kind}})


def hx_empty_with_toast(
    message: str,
    kind: str = "success",
    *,
    status_code: int = 200,
    extra_events: dict[str, Any] | None = None,
) -> HTMLResponse:
    """
    Build an empty-body HTMLResponse that fires a toast (and optional extra
    HX-Trigger events) on success. Used by destructive actions where the
    HTMX target is swapped out and the only feedback the user gets is the
    toast.

    Example:
        return hx_empty_with_toast("Agent deleted", extra_events={"closeSidePanel": {}})
    """
    events: dict[str, Any] = {"showToast": {"message": message, "type": kind}}
    if extra_events:
        events.update(extra_events)
    return HTMLResponse(
        content="",
        status_code=status_code,
        headers={"HX-Trigger": hx_trigger(events)},
    )
