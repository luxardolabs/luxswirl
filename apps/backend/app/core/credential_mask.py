"""Credentials masked out of a value that is shown, not used.

A check's target or connection string can carry a password: a database URL
(`postgresql://monitor:pw@db/app`), an HTTP basic-auth URL, or a libpq/ODBC
`password=` pair. Wherever such a value is displayed to someone who may not
hold it (a viewer, a page any signed-in user loads), it goes through
`mask_credentials`. The stored value, and what the agent runs, are unchanged.
"""

import re
from typing import overload

_MASK = "***"
_MAX_DEPTH = 64
# A URL's userinfo: everything between the scheme and the last `@` of the authority.
_USERINFO = re.compile(r"(?<=://)[^/?#]*@")
# The keys a secret goes by, in a libpq or ODBC string or a URL's query.
_KEYS = r"(?:ssl)?password|passwd|pwd|secret|token|access_token|api_?key"
# A value: braced (ODBC), quoted, or bare with backslash escapes (libpq). A brace or a
# quote left open runs to the end of the text, so it is masked to the end, and a
# scan never fails and starts again. The loops are possessive, since nothing after
# them can need one back, and a plain loop keeps a frame per character. This runs on
# the event loop, over text a viewer can send, and must stay linear in time and space.
_VALUE = (
    r"\{[^}]*(?:\}|\Z)"
    r"|'(?:\\[\s\S]?|[^'\\])*+(?:'|\Z)"
    r'|"(?:\\[\s\S]?|[^"\\])*+(?:"|\Z)'
    r"|(?:\\.|[^\s;&#])++"
)
_PAIR = re.compile(rf"(?i)(?<![\w.-])({_KEYS})(\s*=\s*)({_VALUE})")
# An ODBC string (`;`-separated): a bare value runs to the `;`, spaces included.
_ODBC_PAIR = re.compile(
    rf"(?i)(?<![\w.-])({_KEYS})(\s*=\s*)(\{{[^}}]*(?:\}}|\Z)|[^;]*)"
)
# A URL in free text: its scheme and host, then everything up to a space or quote.
# The scheme is read from where its run of characters starts, so text run into it
# still leaves the URL cut.
_URL = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z0-9+.-]+://[^/\s'\"<>?#]+)[^\s'\"<>]*")


def mask_url_paths(text: str | None) -> str | None:
    """`text` with each URL in it cut to its scheme and host (`https://ha.lan/...`),
    and any credential masked.

    A notification endpoint's path or query can be its credential (a Home
    Assistant webhook id, a chat-hook token), and an HTTP library's error text
    repeats the whole URL; this is how such text is shown and stored.
    """
    if not text:
        return text
    return _URL.sub(r"\1/...", mask_credentials(text))


@overload
def mask_credentials(value: str) -> str: ...
@overload
def mask_credentials(value: None) -> None: ...
def mask_credentials(value: str | None) -> str | None:
    """`value` with every credential it carries replaced by `***`."""
    if not value:
        return value
    masked = _USERINFO.sub(f"{_MASK}@", value)
    pair = _ODBC_PAIR if ";" in masked and "://" not in masked else _PAIR
    return pair.sub(rf"\1\2{_MASK}", masked)


def mask_nested(value: object, depth: int = 0) -> object:
    """`value` (decoded JSON) with every string in it masked.

    Nesting past `_MAX_DEPTH` is replaced whole: an agent chooses it, and recursing
    to whatever depth it sent would end in a RecursionError.
    """
    if depth > _MAX_DEPTH:
        return _MASK
    if isinstance(value, str):
        return mask_credentials(value)
    if isinstance(value, dict):
        return mask_mapping(value, depth)
    if isinstance(value, list):
        return [mask_nested(item, depth + 1) for item in value]
    return value


def mask_mapping(mapping: dict[str, object], depth: int = 0) -> dict[str, object]:
    """`mapping` with every string in its keys and values masked (`mask_nested`).

    A key can be a credential too: a synthetic step is named for its URL.
    """
    return {
        mask_credentials(key): mask_nested(item, depth + 1)
        for key, item in mapping.items()
    }
