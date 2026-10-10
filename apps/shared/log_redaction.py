"""The log redactor both services pass to `configure_logging(redact=...)`.

Every log line goes through `scrub` before it is written: once for the envelope
(the message and any traceback), once for the caller's fields (`extra=` and the
bound `log_context`), each as a flat dict. It masks credentials wherever they
appear: a connection-string password, a Bearer token, `api_key=`/`auth_key=`/
`password=` pairs and JSON password fields, and the whole value of any field
whose NAME says it is secret (`password`, `api_key`, `token`, ...).

It is the backstop, not the control: a call site still names every field it
writes (`repo.log_fields_explicit`), and logs identifiers, never credentials.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_MASK = "***"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Connection strings: mysql://user:PASSWORD@host -> mysql://user:***@host
    (re.compile(r"://([^:/@\s]+):([^@\s]+)@"), r"://\1:***@"),
    # JSON / Python-literal password fields
    (re.compile(r'"password"\s*:\s*"[^"]+"'), '"password":"***"'),
    (re.compile(r"'password'\s*:\s*'[^']+'"), "'password':'***'"),
    # Bearer tokens
    (re.compile(r"Bearer\s+[\w\-\.]+"), "Bearer ***"),
    # key=value / key: value credentials
    (re.compile(r"api_key[=:]\s*[\w\-]+"), "api_key=***"),
    (re.compile(r"auth_key[=:]\s*[\w\-]+"), "auth_key=***"),
    (re.compile(r"password[=:]\s*[\w\-]+"), "password=***"),
)

# A field whose name contains one of these is masked outright, whatever its value.
_SENSITIVE_KEYS = ("password", "api_key", "auth_key", "bearer", "token", "secret")


def _sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(word in lowered for word in _SENSITIVE_KEYS)


def scrub_text(value: str) -> str:
    """`value` with every credential pattern masked."""
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _scrub_value(value: object) -> object:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return _scrub_mapping(value)
    if isinstance(value, list | tuple):
        return [_scrub_value(item) for item in value]
    return value


def _scrub_mapping(data: Mapping[Any, Any]) -> dict[Any, Any]:
    return {
        key: _MASK if isinstance(key, str) and _sensitive(key) else _scrub_value(value)
        for key, value in data.items()
    }


def scrub(line: dict[str, Any]) -> dict[str, Any]:
    """The redactor: `line` with credentials masked (see the module docstring)."""
    return _scrub_mapping(line)
