"""The name a bulk-imported check gets when none is given."""

import re
from urllib.parse import ParseResult, urlparse

from app.core.target_origin import target_origin

_NUMBERED = re.compile(r" \(\d{1,6}\)\Z")


def bulk_display_name(parsed: ParseResult) -> str:
    """A URL's host and any non-default port, joined by hyphens, at most 255
    characters, read from the URL cut to its scheme, host and port. A viewer reads a
    name as written, so it holds no more than a viewer is shown of the target: no
    path, query or userinfo, which is where a credential can sit.

    Raises:
        ValueError: The URL does not parse, or its port is no number. The parser's own message quotes
            what it read, and that can be a password, so this one says only that.
    """
    url = parsed.geturl()
    try:
        reparsed = urlparse(url)
    except ValueError:
        raise ValueError("The URL could not be parsed") from None
    try:
        port = reparsed.port
    except ValueError:
        raise ValueError("The URL has no valid port") from None
    hostname = urlparse(target_origin(url)).hostname
    if not hostname or hostname == "***":
        hostname = "unknown"
    display_name = hostname.replace(".", "-").replace(":", "-")
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
