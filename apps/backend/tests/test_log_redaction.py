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
from app.shared.log_redaction import scrub, scrub_text

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


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        # The password runs to the LAST @ before the host, @ signs and all.
        (
            "postgresql://monitor:p@ss@db:5432/app",
            "postgresql://monitor:***@db:5432/app",
        ),
        ("mysql://root:a@b@c@host/db", "mysql://root:***@host/db"),
        # A key=value secret is masked up to its delimiter, not its first symbol.
        ("password=p@ss!w0rd done", "password=*** done"),
        ("Password=Hunter2!x;Host=db", "Password=***;Host=db"),
        ("PASSWORD: s3cr3t!, next", "PASSWORD: ***, next"),
        ("API_KEY=sk-live-1!2 tail", "API_KEY=*** tail"),
        ("Auth_Key=luxswirl_rk_ab&cd", "Auth_Key=***&cd"),
        ('{"Password": "Hunter2"}', '"Password":"***"'),
        ("authorization: bearer abc.DEF-123", "authorization: bearer ***"),
    ],
)
def test_scrub_text_masks_the_whole_value_in_any_case(raw: str, masked: str) -> None:
    assert masked in scrub_text(raw), scrub_text(raw)


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        # An empty user name is valid in a PostgreSQL, MySQL or Redis URL.
        ("postgresql://:s3cret@db:5432/x", "postgresql://:***@db:5432/x"),
        ("mysql://:s3cret@db/x", "mysql://:***@db/x"),
        ("redis://:s3cret@cache:6379/0", "redis://:***@cache:6379/0"),
        ("postgresql://:p@ss@db/x", "postgresql://:***@db/x"),
        ("amqp://:s3cret@[fe80::1%eth0]:5672/", "amqp://:***@[fe80::1%eth0]:5672/"),
        ("dsn='postgresql://:s3cret@db/x'", "dsn='postgresql://:***@db/x'"),
        ('{"dsn": "postgresql://:s3cret@db/x"}', '{"dsn": "postgresql://:***@db/x"}'),
        # A percent-encoded user name, `:` or `@` and all.
        ("postgresql://us%3Aer:s3cret@db/x", "postgresql://us%3Aer:***@db/x"),
        ("postgresql://u%40x:s3cret@db/x", "postgresql://u%40x:***@db/x"),
        # The password runs from the first `:`, so a `:` in it is masked too.
        ("postgresql://u:s3cret:more@db/x", "postgresql://u:***@db/x"),
        ("postgresql://u:s3cret@[::1]:5432/x", "postgresql://u:***@[::1]:5432/x"),
        # Every URL in a line, whatever its user name.
        (
            "a postgresql://:pw1@a/x and mysql://u:pw2@b/y",
            "a postgresql://:***@a/x and mysql://u:***@b/y",
        ),
        (
            "https://h/x?user=a&password=s3cret&y=1",
            "https://h/x?user=a&password=***&y=1",
        ),
    ],
)
def test_scrub_text_masks_a_url_password_whatever_the_user_name(
    raw: str, masked: str
) -> None:
    assert scrub_text(raw) == masked


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://:pw1@a/x,mysql://:pw2@b/y",
        "['postgresql://:pw1@a/x','mysql://:pw2@b/y']",
    ],
)
def test_scrub_text_masks_both_of_two_adjoining_urls(raw: str) -> None:
    out = scrub_text(raw)
    assert "pw1" not in out
    assert "pw2" not in out


@pytest.mark.parametrize(
    "text",
    [
        "check web-01 failed: HTTP 503 from https://example.com/health",
        # A user with no password carries no secret: left as it is.
        "ssh://user@host/x",
        "ssh://user@host:22/p@x",
        "postgresql://[::1]:5432/x",
        "mail ops@example.com about postgresql://db/x",
    ],
)
def test_scrub_text_leaves_ordinary_text_alone(text: str) -> None:
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


@pytest.mark.parametrize(
    ("raw", "leaked", "kept"),
    [
        (
            "DETAIL:  Key (key_hash)=($2b$12$abcdef) already exists.",
            "$2b$12$abcdef",
            "Key (key_hash)=(***) already exists.",
        ),
        (
            "DETAIL:  Failing row contains (null, s3cr3tHASH, 2026-10-28).",
            "s3cr3tHASH",
            "Failing row contains (***)",
        ),
        (
            "invalid input for query argument $1: 's3cr3t' (invalid UUID 's3cr3t')",
            "s3cr3t",
            "query argument $1: ***",
        ),
        (
            "ValueError: invalid UUID 's3cr3t': length must be",
            "s3cr3t",
            "invalid UUID '***'",
        ),
        (
            'invalid input syntax for type uuid: "s3cr3t"',
            "s3cr3t",
            'invalid input syntax for type uuid: "***"',
        ),
    ],
)
def test_a_database_error_s_quoted_values_are_masked(
    raw: str, leaked: str, kept: str
) -> None:
    """The columns and the reason stay readable; the values a write bound go."""
    scrubbed = scrub_text(raw)
    assert leaked not in scrubbed, scrubbed
    assert kept in scrubbed, scrubbed


def test_text_that_quotes_no_database_value_is_left_alone() -> None:
    text = "Key rotation done (3 agents); invalid request from 10.0.0.5"
    assert scrub_text(text) == text
