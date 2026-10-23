"""Content-Disposition values built from a filename that may not be ours.

A filename spliced into `filename="…"` breaks out of the quoted string at a `"`
(adding its own parameters) and splits the header at a CR or LF. RFC 6266 gives
the safe form: an ASCII `filename` fallback with everything outside a
conservative set replaced, and the exact name percent-encoded in `filename*`.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import quote

# What may appear in the quoted ASCII fallback: plain filename characters only,
# so it can never read as a quote, a separator or another parameter.
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()-]")


def content_disposition(
    disposition: Literal["inline", "attachment"], filename: str
) -> str:
    """The header value for serving `filename`, safe whatever it contains."""
    fallback = _UNSAFE.sub("_", filename).strip(" _") or "download"
    encoded = quote(filename, safe="")
    if encoded == filename:
        return f'{disposition}; filename="{filename}"'
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
