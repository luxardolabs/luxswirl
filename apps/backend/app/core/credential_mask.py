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
# The keys a secret goes by, in a libpq or ODBC string or a URL's query.
_KEYS = r"(?:ssl)?password|passwd|pwd|secret|token|access_token|api_?key"
# A value: braced (ODBC), quoted, or bare with backslash escapes (libpq).
_VALUE = r"\{[^}]*\}|'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"|(?:\\.|[^\s;&#])+"
_PAIR = re.compile(rf"(?i)(?<![\w.-])({_KEYS})(\s*=\s*)({_VALUE})")
# An ODBC string (`;`-separated): a bare value runs to the `;`, spaces included.
_ODBC_PAIR = re.compile(rf"(?i)(?<![\w.-])({_KEYS})(\s*=\s*)(\{{[^}}]*\}}|[^;]*)")


def mask_credentials(value: str | None) -> str | None:
    """`value` with every credential it carries replaced by `***`."""
    if not value:
        return value
    masked = _USERINFO.sub(f"{_MASK}@", value)
    pair = _ODBC_PAIR if ";" in masked and "://" not in masked else _PAIR
    return pair.sub(rf"\1\2{_MASK}", masked)
