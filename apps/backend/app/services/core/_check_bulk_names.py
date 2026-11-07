"""The name a bulk-imported check gets when none is given."""

import re
from urllib.parse import ParseResult, urlparse

from app.core.credential_mask import mask_credentials

_NUMBERED = re.compile(r" \(\d{1,6}\)\Z")


def bulk_display_name(parsed: ParseResult) -> str:
    """A URL's host, path and any non-default port, joined by hyphens, at most 255
    characters, all read from the masked URL. A viewer reads a name as written, so it
    holds no more than the masked target shows.

    Raises:
        ValueError: The port is no number. The parser's own message quotes it, and a
            password holding a `/` can read as one, so this one says only that.
    """
    masked = urlparse(mask_credentials(parsed.geturl()))
    hostname = masked.hostname or "unknown"
    display_name = hostname.replace(".", "-")
    if masked.path and masked.path != "/":
        path_part = masked.path.strip("/").replace("/", "-")
        display_name = f"{display_name}-{path_part}"
    try:
        port = masked.port
    except ValueError:
        raise ValueError("The URL has no valid port") from None
    if port and port not in (80, 443):
        display_name = f"{display_name}-{port}"
    return display_name[:255]


def unused_name(name: str, taken: set[str], numbers: dict[str, int]) -> str:
    """`name`, numbered when an agent's check has it already: masking leaves webhooks on
    one host with one name, and an import and the metrics tell checks by theirs. A number
    already on it is replaced, so the names a preview numbered stay single numbered."""
    if name not in taken:
        return name
    base = _NUMBERED.sub("", name)
    unused = base
    number = numbers.get(base, 1)
    while unused in taken:
        number += 1
        suffix = f" ({number})"
        unused = f"{base[: 255 - len(suffix)]}{suffix}"
    numbers[base] = number
    return unused
