"""
The app takes a multipart form body in UTF-8 only.

Starlette decodes each multipart field with the charset the request's
Content-Type names. A codec such as unicode_escape turns the field bytes
`\\ud800` into a lone surrogate: text UTF-8 cannot encode, so no page can echo
it back, no column can store it and no log line can write it. A browser names
no charset on a multipart body, so a body that names one other than UTF-8 is
refused before any route runs. A urlencoded body is always decoded as UTF-8,
with any byte it cannot decode replaced, whatever charset it names.
"""

import codecs

from fastapi import Request
from python_multipart.multipart import parse_options_header

from app.core.exceptions import UnsupportedMediaTypeException


def _is_utf8(charset: bytes) -> bool:
    try:
        return codecs.lookup(charset.decode("latin-1")).name == "utf-8"
    except LookupError:
        return False


async def require_utf8_form_body(request: Request) -> None:
    """
    Refuse a multipart body whose Content-Type names a charset other than UTF-8.

    Args:
        request: The incoming request

    Raises:
        UnsupportedMediaTypeException: If the multipart body names another charset
    """
    media_type, params = parse_options_header(request.headers.get("content-type", ""))
    charset = params.get(b"charset")
    if (
        media_type.lower() == b"multipart/form-data"
        and charset is not None
        and not _is_utf8(charset)
    ):
        raise UnsupportedMediaTypeException("A multipart form body must be UTF-8")
