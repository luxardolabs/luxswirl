"""Content-Disposition is safe whatever filename an agent uploaded."""

from __future__ import annotations

import pytest
from app.core.content_disposition import content_disposition

pytestmark = pytest.mark.unit


def test_a_plain_name_is_quoted_as_is() -> None:
    assert content_disposition("inline", "shot.png") == 'inline; filename="shot.png"'


@pytest.mark.parametrize(
    "name",
    [
        'x.png"; filename="evil.html',  # breaks out of the quoted string
        "x.png\r\nSet-Cookie: a=b",  # splits the header
        "a\\b.png",
        "résumé.png",
    ],
)
def test_a_hostile_or_non_ascii_name_cannot_escape_the_value(name: str) -> None:
    value = content_disposition("attachment", name)

    assert "\r" not in value and "\n" not in value
    # Exactly the two parameters this function writes, nothing injected.
    assert value.count("filename=") == 1
    assert value.count("filename*=") == 1
    quoted = value.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in quoted and "\\" not in quoted


def test_an_empty_name_still_gives_a_filename() -> None:
    assert 'filename="download"' in content_disposition("attachment", "\r\n")
