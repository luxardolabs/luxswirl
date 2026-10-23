"""The log redactor, alone and wired into the line the server actually writes.

`shared.log_redaction.scrub` replaced the old `CredentialFilter`; it must mask
the same credentials, wherever in the line they sit: the message, a traceback,
an `extra=` field, a nested value, or a field whose name alone says secret.
"""

from __future__ import annotations

import json
import logging
import sys

import pytest
from app.core.logging_config import JsonFormatter
from app.core.logging_options import logging_options
from shared.log_redaction import scrub, scrub_text

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        ("postgresql://svc:hunter2@db:5432/x", "hunter2"),
        ("mysql://root:p@ss:word@host/db", "p@ss"),
        ("Authorization: Bearer abc.DEF-123", "abc.DEF-123"),
        ("api_key=sk-live-999", "sk-live-999"),
        ("auth_key: luxswirl_rk_deadbeef", "luxswirl_rk_deadbeef"),
        ("password=letmein", "letmein"),
        ("recovered with luxswirl_rs_0a1b2c3d4e5f", "0a1b2c3d4e5f"),
        ('{"sent": "luxswirl_rs_xyz"}', "_xyz"),
        ('{"password": "letmein"}', "letmein"),
        ("{'password': 'letmein'}", "letmein"),
    ],
)
def test_scrub_text_masks_every_credential_shape(raw: str, leaked: str) -> None:
    assert leaked not in scrub_text(raw)


def test_scrub_text_leaves_ordinary_text_alone() -> None:
    text = "check web-01 failed: HTTP 503 from https://example.com/health"
    assert scrub_text(text) == text


def test_a_secret_named_field_is_masked_whatever_its_value() -> None:
    out = scrub({"auth_token": "abc", "API_KEY": 123, "client_secret": None})
    assert out == {"auth_token": "***", "API_KEY": "***", "client_secret": "***"}


def test_nested_values_and_lists_are_scrubbed() -> None:
    out = scrub(
        {
            "target": {"dsn": "postgresql://u:pw1@h/d", "password": "x"},
            "urls": ["mysql://a:pw2@h/d", 7, ("Bearer tok3",)],
        }
    )
    flat = json.dumps(out, default=str)
    for leaked in ("pw1", "pw2", "tok3", '"x"'):
        assert leaked not in flat
    assert out["urls"][1] == 7


def test_non_string_keys_and_values_survive() -> None:
    assert scrub({"n": 1, "ok": True, 3: "three", "none": None}) == {
        "n": 1,
        "ok": True,
        3: "three",
        "none": None,
    }


def _line(record: logging.LogRecord) -> dict[str, object]:
    options = logging_options()
    formatter = JsonFormatter(
        options["service"],
        options["version"],
        options["environment"],
        options["redact"],
    )
    return json.loads(formatter.format(record))


def _record(msg: str, extra: dict[str, object] | None = None, exc: bool = False):
    exc_info = None
    if exc:
        try:
            raise RuntimeError("connect to postgresql://svc:tracepw@db/x failed")
        except RuntimeError:
            exc_info = sys.exc_info()
    record = logging.LogRecord(
        "luxswirl.test", logging.ERROR, __file__, 1, msg, (), exc_info
    )
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def test_the_server_line_is_redacted_in_message_fields_and_traceback() -> None:
    line = _line(
        _record(
            "login with Bearer msgtok failed",
            extra={"password": "fieldpw", "dsn": "mysql://u:dsnpw@h/d"},
            exc=True,
        )
    )
    text = json.dumps(line)
    for leaked in ("msgtok", "fieldpw", "dsnpw", "tracepw"):
        assert leaked not in text
    assert line["service"] == "luxswirl"
    assert line["level"] == "ERROR"
    assert line["exception_type"] == "RuntimeError"


def test_server_options_carry_the_redactor_and_the_level_defaults() -> None:
    options = logging_options()
    assert options["redact"] is scrub
    assert options["levels"]["sqlalchemy.engine"] == logging.WARNING
