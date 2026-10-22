# luxarch:safe-redirect asset v2 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit safe-redirect`.
"""The one safe-redirect helper (FLEET-AUTH-SESSION-STANDARD §6).

Every user-supplied redirect target (`next`, `return_to`, a `Referer` fallback) goes through
``safe_redirect_target`` before it reaches ``RedirectResponse``, a ``Location`` header or an HTMX
``HX-Redirect`` / ``HX-Location``. It returns the value only when it is a path on THIS site, and
the default otherwise, so a link such as ``/login?next=//evil.example`` cannot send a signed-in
user anywhere else. ``fw.redirect_target_safe`` recognises this function by its import, not its name.

    from app.core.safe_redirect import safe_redirect_target

    return RedirectResponse(safe_redirect_target(next), status_code=303)

An OAuth ``redirect_uri`` is a different check: it is matched exactly against the registered list.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

DEFAULT_TARGET = "/"


def safe_redirect_target(value: object, default: str = DEFAULT_TARGET) -> str:
    """``value`` when it is a path on this site (``/account``, ``/items?page=2``), else ``default``.

    Refused: anything that is not a string; a scheme or host (``https://x``, ``//x``); a backslash,
    which browsers read as a slash (``/\\x``); control characters and anything outside printable ASCII (the value goes into a header); and the same tricks once
    percent-decoded (``/%2F%2Fx``).
    """
    if not isinstance(value, str):
        return default
    v = value.strip()
    # v2: what is returned goes into a header (`Location`, HTMX `HX-Redirect`), which must be latin-1:
    # a raw path holding U+2028 LINE SEPARATOR passed every check below and 500'd the HX-Redirect. A path a browser sends is
    # percent-encoded ASCII, so anything outside printable ASCII (space included) is refused; an
    # encoded non-ASCII path (`/caf%C3%A9`) is still a path.
    if any(not 0x21 <= ord(c) <= 0x7E for c in v):
        return default
    for candidate in (v, unquote(v)):
        if not candidate.startswith("/") or candidate.startswith("//"):
            return default
        if "\\" in candidate or any(ord(c) < 0x20 or ord(c) == 0x7F for c in candidate):
            return default
        parts = urlsplit(candidate)
        if parts.scheme or parts.netloc:
            return default
    return v
