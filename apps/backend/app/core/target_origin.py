"""A check's target cut to where it points: its scheme, host and port, nothing else.

`mask_credentials` works from a list of the forms a credential takes, and a list of
that kind never ends: an unlisted query key, a token in an ordinary path segment, a
double-encoded value are shown as written. So a reader who does not hold the
credential (a viewer, the unauthenticated `/metrics`) is not shown the target through
the list at all. It is parsed, and only the parts that cannot carry a secret are kept:
`https://user:pw@api.example.com:8443/v1?token=x` is shown as
`https://api.example.com:8443`.

The parse fails closed. When the text cannot be read as a host and a port with
certainty, the host is shown as `***`: an `@` anywhere after where the authority
seems to end (a userinfo holding `/`, `?`, `#`, `;` or a space splits it early, and
the real host follows that `@`), an IPv6 zone, a port that is no number.

Forms read:
- A URL, `scheme://[userinfo@]host[:port][/path][?query]`, JDBC's
  `jdbc:postgresql://` and SQL Server's `;key=value` tail included.
- A `key=value` connection string (libpq, ODBC, ADO): only its `host` / `server` /
  `data source` and `port` are kept, as `host=... port=...`.
- A bare host, `host:port` or `[ipv6]:port`, with userinfo before an `@` dropped.

Linear in the length of the text: each step is a scan or a split, never a pattern
that can backtrack.
"""

import ipaddress
import re
from typing import overload

from app.core.credential_mask import mask_credentials
from app.models.enum_model import UserRole

_HIDDEN = "***"

_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*(?::[A-Za-z][A-Za-z0-9+.-]*)?")
_HOST_NAME = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,252})")
_PORT = re.compile(r"[0-9]{1,5}")

# Where an authority ends: a path, query, fragment, SQL Server's `;` pairs, a space.
_AUTHORITY_END = frozenset("/?#;\\ \t\r\n\f\v")
# A key=value string's first key: a word, so `host/x?a=b` is not read as pairs.
_FIRST_KEY = re.compile(r"[;\s]*[A-Za-z][A-Za-z0-9 _]*\s*=")

# The keys that say where a connection string connects, by dialect. An ODBC or ADO
# string (`;`-separated, a value runs to the next `;`) names its host `Server`; a
# libpq one (space-separated, a value runs to the next unescaped space) `host`. A key
# of the other dialect is not read, so a password that holds the text of one is not
# read as a host.
_ODBC_HOST_KEYS = frozenset(
    {"server", "data source", "datasource", "address", "addr", "network address"}
)
_LIBPQ_HOST_KEYS = frozenset({"host", "hostaddr"})
_PORT_KEYS = frozenset({"port"})


def _first_index(text: str, stops: frozenset[str], start: int = 0) -> int:
    """The index of the first character of `text` at or after `start` in `stops`."""
    for index in range(start, len(text)):
        if text[index] in stops:
            return index
    return len(text)


def _host(text: str) -> str | None:
    """`text` as a host name or IP address, lower-cased, or None. An IPv6 zone
    (`%eth0`) takes any text, so an address with one is not read."""
    if "%" in text:
        return None
    if text.startswith("[") and text.endswith("]"):
        try:
            return f"[{ipaddress.IPv6Address(text[1:-1]).compressed}]"
        except ValueError:
            return None
    if ":" in text:
        try:
            return f"[{ipaddress.IPv6Address(text).compressed}]"
        except ValueError:
            return None
    if _HOST_NAME.fullmatch(text):
        return text.lower()
    return None


def _port(text: str) -> str | None:
    """`text` as a TCP port, 1 to 65535, or None."""
    if _PORT.fullmatch(text) and 0 < int(text) < 65536:
        return str(int(text))
    return None


def _host_and_port(text: str) -> str | None:
    """`host`, `host:port` or `[v6]:port` read from `text`, or None when any part of it
    is not one."""
    if text.startswith("["):
        close = text.find("]")
        if close < 0:
            return None
        host, rest = _host(text[: close + 1]), text[close + 1 :]
        if host is None:
            return None
        if not rest:
            return host
        port = _port(rest[1:]) if rest.startswith(":") else None
        return f"{host}:{port}" if port else None
    if text.count(":") == 1:
        name, _, number = text.partition(":")
        host, port = _host(name), _port(number)
        return f"{host}:{port}" if host and port else None
    return _host(text)


def _authority(rest: str) -> str | None:
    """The host and port of `rest`, the text after a scheme's `://` (or a bare
    target), its userinfo dropped; None when they cannot be read with certainty."""
    end = _first_index(rest, _AUTHORITY_END)
    authority, after = rest[:end], rest[end:]
    # A userinfo holding one of the authority's end characters splits it early, and
    # the real host follows an `@` somewhere after the split. Nothing tells that `@`
    # from one in a query value (`?email=ops@example.com`), so any `@` after the
    # split means the host is not known.
    if "@" in after:
        return None
    if "@" in authority:
        authority = authority.rpartition("@")[2]
    if not authority:
        return None
    return _host_and_port(authority)


def _quoted_end(text: str, start: int, close: str) -> int:
    """The index after the value quoted from `start` (its opening quote) to `close`:
    a doubled `close` (ODBC's `}}`, SQL's `''`) or a backslash (libpq) escapes it."""
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\" and close == "'":
            index += 2
            continue
        if char == close:
            if text[index + 1 : index + 2] == close:
                index += 2
                continue
            return index + 1
        index += 1
    return len(text)


def _key_values(text: str, odbc: bool) -> list[tuple[str, str]]:
    """The (key, value) pairs of a libpq, ODBC or ADO connection string, read as its
    driver reads them: a quoted or braced value whole; an unquoted one to the next
    `;` (ODBC, `odbc`) or to the next space no backslash escapes (libpq). So a
    `host=` inside a password is not read as a key."""
    pairs: list[tuple[str, str]] = []
    index = 0
    while index < len(text):
        while index < len(text) and (text[index] in ";" or text[index].isspace()):
            index += 1
        equals = text.find("=", index)
        if equals < 0:
            break
        key = " ".join(text[index:equals].split()).lower()
        start = equals + 1
        while start < len(text) and text[start] in " \t":
            start += 1
        opening = text[start : start + 1]
        if opening in ("'", '"', "{"):
            end = _quoted_end(text, start, "}" if opening == "{" else opening)
            value = text[start + 1 : end - 1]
        else:
            end = start
            while end < len(text):
                char = text[end]
                if (char == ";") if odbc else char.isspace():
                    break
                end += 2 if (char == "\\" and not odbc) else 1
            end = min(end, len(text))
            value = text[start:end]
        pairs.append((key, value))
        index = end
    return pairs


def _pairs(value: str) -> str:
    """A key=value connection string cut to its host and port keys."""
    odbc = ";" in value
    host_keys = _ODBC_HOST_KEYS if odbc else _LIBPQ_HOST_KEYS
    hosts: list[str] = []
    ports: list[str] = []
    for name, text in _key_values(value, odbc):
        if name in host_keys:
            hosts.append(text)
        elif name in _PORT_KEYS:
            ports.append(text)
    shown: list[str] = []
    for text in hosts:
        # ODBC writes `tcp:host,1433`; libpq a comma-separated host list.
        for part in text.split(","):
            if part.lower().startswith(("tcp:", "np:", "lpc:")):
                part = part.partition(":")[2]
            host = _host_and_port(part) if part else None
            shown.append(host or _HIDDEN)
    for text in ports:
        shown.extend(
            f"port={_port(part) or _HIDDEN}" for part in text.split(",") if part
        )
    hosts_shown = [part for part in shown if not part.startswith("port=")]
    ports_shown = [part for part in shown if part.startswith("port=")]
    if not hosts_shown:
        return _HIDDEN
    return " ".join([f"host={','.join(hosts_shown)}", *ports_shown])


@overload
def target_origin(value: str) -> str: ...
@overload
def target_origin(value: None) -> None: ...
def target_origin(value: str | None) -> str | None:
    """`value`, a check's target or connection string, cut to its scheme, host and
    port: what a reader who does not hold its credentials is shown."""
    if not value:
        return value
    text = value.strip()
    scheme_end = text.find("://")
    if scheme_end >= 0:
        scheme = text[:scheme_end]
        if not _SCHEME.fullmatch(scheme):
            return _HIDDEN
        host = _authority(text[scheme_end + 3 :])
        return f"{scheme.lower()}://{host or _HIDDEN}"
    if _FIRST_KEY.match(text):
        return _pairs(text)
    return _authority(text) or _HIDDEN


_HOLDERS = frozenset({UserRole.ADMIN, UserRole.EDITOR})


@overload
def shown_target(value: str, role: UserRole | None) -> str: ...
@overload
def shown_target(value: None, role: UserRole | None) -> None: ...
def shown_target(value: str | None, role: UserRole | None) -> str | None:
    """A check's target or connection string as `role` is shown it: an editor or
    admin, who can open the check's form and read it whole, through
    `mask_credentials`; anyone else (a viewer, no one signed in) its scheme, host and
    port alone."""
    if role in _HOLDERS:
        return mask_credentials(value)
    return target_origin(value)
