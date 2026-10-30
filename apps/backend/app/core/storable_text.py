"""
Whether a column can hold a piece of text.

PostgreSQL refuses a NUL in text and JSONB, and UTF-8 cannot encode a lone
surrogate (which a JSON escape such as `"\\ud800"` decodes to), so no page,
column or log line can hold either.
"""


def storable_text(text: str) -> bool:
    """
    Whether a column can hold the text.

    Args:
        text: The text to judge

    Returns:
        False if it holds a NUL or a lone surrogate
    """
    if "\x00" in text:
        return False
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True
