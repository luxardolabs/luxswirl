"""
Shared HTMX response helpers for web routers.

Multi-consumer helper — every web router that emits `HX-Trigger` events
alongside an empty or partial body builds the header here, so the
`{event: detail}` shape and the `showToast` contract live in one place.
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


def hx_toast_without_swap(message: str, kind: str = "error") -> HTMLResponse:
    """Report an outcome without letting HTMX swap the target away.

    `HX-Reswap: none` overrides the element's own hx-swap, so a failed
    destructive action leaves the row it targeted on screen next to the toast
    explaining why nothing happened.
    """
    response = hx_empty_with_toast(message, kind=kind)
    response.headers["HX-Reswap"] = "none"
    return response
