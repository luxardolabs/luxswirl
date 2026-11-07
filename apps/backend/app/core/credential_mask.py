"""Credentials masked out of a value that is shown, not used.

A check's target or connection string can carry a credential: a database URL
(`postgresql://monitor:pw@db/app`), a query string (`?client_secret=...`), a libpq or
ODBC pair, or a webhook's path. Wherever such a value is displayed to someone who may
not hold it (a viewer, a page any signed-in user loads, an unauthenticated `/metrics`),
it goes through `mask_credentials`. The stored value, and what the agent runs, are
unchanged.

It works from a list of the forms a credential takes, so a form not on the list is
shown, as is a credential in an ordinary path segment, which cannot be told from a word.

Every pattern runs on the event loop, over text a viewer can send, and must stay linear
in time and space. So a loop that reads to the end of a token is anchored to the token's
start, and one that nothing after it can need back is possessive, since a plain loop
keeps a frame per character.
"""

import re
from typing import overload

_MASK = "***"
_MAX_DEPTH = 64
# A URL's userinfo: everything between the scheme and the last `@` of the authority.
_USERINFO = re.compile(r"(?<=://)[^/?#]*@")

_KEY_CHAR = r"[\w.%\[\]-]"
# Words that name a secret wherever they sit in a key (`clientsecret`, `AUTH_TOKEN`).
_SECRET_ANYWHERE = (
    r"passw|passphrase|passhash|passkey|bindpw|dbpass|userpass|adminpass|pwd|secret"
    r"|token|credential|assertion|appid|sess(?:ion)?id|odbc_connect"
    r"|(?:api|app|access|account|auth|client|license|private|shared|signing|subscription"
    r"|master|session|encryption)key"
)
# Words that do only as a whole word, in a key split by `_`, `-`, `.`, an encoded one of
# those, or case (`x-api-key`, `AccountKey`, `api%2Dkey`), so `monkey` and `design` stay.
_SECRET_WORD = (
    r"keys?\d*|sig|signature|pw|psw|pswd|pass\d*|passcode|auth|authorization|bearer"
    r"|jwt|hmac|session|sid|cookie|creds?"
)
# Keys built from those words that name a setting, not a secret.
_SETTING = (
    r"(?i:auth_?(?:source|mechanism|plugin|type|method)|session_?(?:timeout|length|ttl)"
    r"|token_?(?:type|endpoint|url)|allowPublicKeyRetrieval|authMechanismProperties"
    r"|auth[_-](?:host|port|url|source|db)|(?:routing|primary|sort|partition)_?key"
    rf"|(?<=\()sid)(?!{_KEY_CHAR})"
)


def _secret_key(ending: str) -> str:
    """A pattern for a key naming a secret, followed by `ending`.

    The lookahead and the atomic group keep a run of such words from being read again
    from each one: a run without `ending` is passed over, and the first word found
    settles the key, whatever follows it.
    """
    return (
        rf"(?={_KEY_CHAR}++{ending})(?!{_SETTING})(?>{_KEY_CHAR}*?"
        rf"(?:(?i:{_SECRET_ANYWHERE})"
        r"|(?:(?<![A-Za-z0-9])|(?<=[a-z0-9])(?=[A-Z])|(?<=%[0-9A-Fa-f]{2}))"
        rf"(?i:{_SECRET_WORD})(?![a-z0-9]))"
        rf"{_KEY_CHAR}*+)"
    )


_KEY_AND_SEPARATOR = rf"(?<![\w.%\[\]-])({_secret_key(r'\s*=')})(\s*=\s*)"
# A value: braced (ODBC, `}}` an escaped brace), quoted, or bare with backslash escapes
# (libpq). A brace or a quote left open runs to the end of the text, so it is masked to
# the end, and a scan never fails and starts again.
_BRACED = r"\{\{[^}]*+\}\}|\{(?:[^}]|\}\})*+(?:\}|\Z)"
_LIBPQ_BRACED = r"\{[^}]*+(?:\}|\Z)"
_QUOTED = r"'(?:\\[\s\S]?|[^'\\])*+(?:'|\Z)" r'|"(?:\\[\s\S]?|[^"\\])*+(?:"|\Z)'
_BARE = r"(?:\\.|[^\s;&#])++"
_PAIR = re.compile(
    rf"{_KEY_AND_SEPARATOR}((?:{_LIBPQ_BRACED}|{_QUOTED})(?:\\.|[^\s;&#])*+|{_BARE})"
)
# An ODBC string (`;`-separated): a bare value runs to the `;`, spaces included.
_ODBC_PAIR = re.compile(
    rf"{_KEY_AND_SEPARATOR}({_BRACED}|(?:{_QUOTED})(?=\s*+(?:;|\Z))|[^;]*+)"
)
# The narrow keys go first, so a value under a wider key cannot swallow one's name.
_NAMED = r"(?i)(?<![\w.-])((?:ssl)?password|passwd|pwd|secret|token|access_token|api_?key)(\s*=\s*)"
_NAMED_PAIR = re.compile(rf"{_NAMED}({_LIBPQ_BRACED}|{_QUOTED}|{_BARE})")
_NAMED_ODBC_PAIR = re.compile(
    rf"{_NAMED}({_BRACED}|(?:{_QUOTED})(?=\s*+(?:;|\Z))|[^;]*+)"
)
# An OAuth code or an Azure Functions key; in other text, or all digits, `code=` is a status.
_QUERY_CODE = re.compile(r"(?<=[?&])(code=)(?!\d++(?:[&#\s]|\Z))[^\s&#]*+")

_PATH_CHAR = r"[^\s/?#'\"<>]"
# A path that is itself the credential: a webhook's, by its host or a segment's name.
_WEBHOOK_PATH = re.compile(
    r"(?i)((?<=://)(?>[^/\s]*?(?:webhook|hooks?\.))[^/\s]*+"
    r"|(?<=/)(?>[\w-]*?webhook[\w-]*+|hooks?))"
    rf"/{_PATH_CHAR}[^\s?#'\"<>]*+"
)
_SLACK_PATH = re.compile(rf"/services/T[A-Z0-9]++/B[A-Z0-9]++/{_PATH_CHAR}++")
_BOT_PATH = re.compile(r"(?i)(?<!//bot)(?<=/bot)\d++:(?!\d++(?:[/?#\s]|\Z))[\w-]++")
# Text with none of these holds no form above, and most of what a document holds is such.
_MAY_HOLD_A_CREDENTIAL = re.compile(r"[=/@]")

# A URL in free text: its scheme and host, then everything up to a space or quote.
# The scheme is read from where its run of characters starts, so text run into it
# still leaves the URL cut.
_URL = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z0-9+.-]+://[^/\s'\"<>?#]+)[^\s'\"<>]*")


def _named(value: str) -> str:
    """`value` with its userinfo and the values of the narrow keys masked."""
    masked = _USERINFO.sub(f"{_MASK}@", value)
    odbc = ";" in masked and "://" not in masked
    return (_NAMED_ODBC_PAIR if odbc else _NAMED_PAIR).sub(rf"\1\2{_MASK}", masked)


def mask_url_paths(text: str | None) -> str | None:
    """`text` with each URL in it cut to its scheme and host (`https://ha.lan/...`),
    and any credential masked.

    A notification endpoint's path or query can be its credential (a Home
    Assistant webhook id, a chat-hook token), and an HTTP library's error text
    repeats the whole URL; this is how such text is shown and stored.
    """
    if not text:
        return text
    first = _URL.sub(r"\1/...", _named(text))
    return _URL.sub(r"\1/...", mask_credentials(first))


@overload
def mask_credentials(value: str) -> str: ...
@overload
def mask_credentials(value: None) -> None: ...
def mask_credentials(value: str | None) -> str | None:
    """`value` with every credential it carries replaced by `***`."""
    if not value:
        return value
    if not _MAY_HOLD_A_CREDENTIAL.search(value):
        return value
    masked = _named(value)
    pair = _ODBC_PAIR if ";" in masked and "://" not in masked else _PAIR
    masked = pair.sub(rf"\1\2{_MASK}", masked)
    masked = _QUERY_CODE.sub(rf"\1{_MASK}", masked)
    masked = _WEBHOOK_PATH.sub(rf"\1/{_MASK}", masked)
    masked = _SLACK_PATH.sub(f"/services/{_MASK}", masked)
    return _BOT_PATH.sub(_MASK, masked)


_SECRET_KEY_NAME = re.compile(_secret_key(r"\Z"))
# The most of a key judged: enough for any real name, and a key can be 4 MiB.
_KEY_LENGTH = 256


class KeyNames:
    """One name for each key of one document, a credential in it masked.

    A key can be a credential: a synthetic step is named for its URL. Keys that mask
    alike stay apart, numbered by which came first, so no name depends on how the
    secrets sort, and a key reads the same in every mapping of the document.
    """

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._used: set[str] = set()
        self._numbers: dict[str, int] = {}

    def of(self, key: str, shown: str | None = None) -> str:
        """The name of `key`; `shown` is the text to name it by, when that is not `key`."""
        if key not in self._names:
            masked = mask_credentials(key if shown is None else shown)
            name = masked
            number = self._numbers.get(masked, 1)
            while name in self._used:
                number += 1
                name = f"{masked} ({number})"
            self._numbers[masked] = number
            self._used.add(name)
            self._names[key] = name
        return self._names[key]


def names_a_secret(key: str) -> bool:
    """Whether a key says a string under it is a secret. Each word is judged (`API Key`)."""
    return any(_SECRET_KEY_NAME.fullmatch(word) for word in key[:_KEY_LENGTH].split())


def _masked_items(
    mapping: dict[str, object], names: KeyNames, depth: int, hidden: bool
) -> dict[str, object]:
    return {
        names.of(key): _masked(item, names, depth, hidden or names_a_secret(key))
        for key, item in mapping.items()
    }


def _masked(value: object, names: KeyNames, depth: int, hidden: bool) -> object:
    if depth > _MAX_DEPTH:
        return _MASK
    if isinstance(value, str):
        return _MASK if hidden and value else mask_credentials(value)
    if isinstance(value, dict):
        return _masked_items(value, names, depth + 1, hidden)
    if isinstance(value, list):
        return [_masked(item, names, depth + 1, hidden) for item in value]
    return value


def mask_mapping(mapping: dict[str, object]) -> dict[str, object]:
    """`mapping` (decoded JSON) with every string in its keys and values masked, and every
    string under a key that names a secret hidden whole, however deep. Its shape and its
    figures stay, since a page reads them by name and a key can be a step someone named
    `get_token`.

    Nesting past `_MAX_DEPTH` is replaced whole: an agent chooses it, and recursing
    to whatever depth it sent would end in a RecursionError.
    """
    return _masked_items(mapping, KeyNames(), 1, False)
