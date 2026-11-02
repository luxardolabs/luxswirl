"""
Whether a header can be sent on an HTTP request.

A name is an RFC 7230 token. A value is printable ASCII, spaces and tabs,
with no space or tab at either end: httpx encodes a value as ASCII, and h11
refuses a CR, LF, NUL or any other control character, and a value that starts
or ends with whitespace. h11 refuses only once the connection is open, and
leaves that connection unclosed, so a header is judged before any request is
built.
"""

import re

# RFC 7230 section 3.2.6: token = 1*tchar.
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# RFC 7230 section 3.2: printable ASCII, with inner spaces and tabs.
_VALUE = re.compile(r"([\x21-\x7e]([\x20-\x7e\t]*[\x21-\x7e])?)?")


class HeaderRefusedError(ValueError):
    """A header HTTP cannot send. Its message is the app's own: it names a
    valid header name, never a value or a refused name, which can be a
    credential or hold a line break."""


def sendable_headers(field: str, headers: object) -> dict[str, str]:
    """
    The headers, if every one can be sent.

    Args:
        field: The config field the headers came from, named in a refusal
        headers: The headers as configured

    Returns:
        The headers

    Raises:
        HeaderRefusedError: If they are not an object of text, or a name or
            value cannot be sent
    """
    if not isinstance(headers, dict):
        raise HeaderRefusedError(f"{field} must be a dictionary")
    sendable: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not _TOKEN.fullmatch(name):
            raise HeaderRefusedError(
                f"{field}: a header name must be letters, digits and "
                "!#$%&'*+-.^_`|~ only, with no space or control character"
            )
        if not isinstance(value, str):
            raise HeaderRefusedError(f"{field}: header {name} must be text")
        if not _VALUE.fullmatch(value):
            raise HeaderRefusedError(
                f"{field}: header {name} must be printable ASCII, with no "
                "CR, LF, NUL or other control character and no space at "
                "either end"
            )
        sendable[name] = value
    return sendable
