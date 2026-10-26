"""Credentials masked out of a value that is shown, not used.

A check's target or connection string can carry a password: a database URL
(`postgresql://monitor:pw@db/app`), an HTTP basic-auth URL, or a libpq/ODBC
`password=` pair. Wherever such a value is displayed to someone who may not
hold it (a viewer, a page any signed-in user loads), it goes through
`mask_credentials`. The stored value, and what the agent runs, are unchanged.
"""

import re

_MASK = "***"
# A URL's userinfo: everything between the scheme and the last `@` of the authority.
_USERINFO = re.compile(r"(?<=://)[^/?#]*@")
# A key=value password, as libpq, ODBC and query strings write one.
_PASSWORD_PAIR = re.compile(
    r"(?i)\b(password|passwd|pwd)(\s*=\s*)('[^']*'|\"[^\"]*\"|[^\s;&]+)"
)


def mask_credentials(value: str | None) -> str | None:
    """`value` with every credential it carries replaced by `***`."""
    if not value:
        return value
    masked = _USERINFO.sub(f"{_MASK}@", value)
    return _PASSWORD_PAIR.sub(rf"\1\2{_MASK}", masked)
