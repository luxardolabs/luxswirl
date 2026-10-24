"""The check panel's certificate card says what is known about the expiry.

A certificate whose expiry could not be read is the one an operator most needs
to see, so it reads as Unknown, never as a green Valid.
"""

from __future__ import annotations

import re

import pytest
from app.core.ssl_expiry import ssl_expiry_fields
from app.services.views.check_detail_view_service import SSL_CERT_TONES
from app.web.templates import templates

pytestmark = pytest.mark.unit

CARD = "partials/panels/checks/metrics/ssl_certificate.html"


def _render(expiration_date: str | None) -> str:
    ssl_cert = {
        "subject": "CN=example.test",
        "issuer": "CN=Test CA",
        "expiration_display": expiration_date or "Unknown",
        **ssl_expiry_fields(expiration_date, warning_days=30, critical_days=7),
    }
    ssl_cert["tone"] = SSL_CERT_TONES[ssl_cert["state"]]
    html = templates.get_template(CARD).render(ssl_cert=ssl_cert)
    return re.sub(r"\s+", " ", html)


def test_an_unreadable_expiry_reads_as_unknown_not_valid() -> None:
    html = _render("not a date")

    assert ">Unknown<" in html.replace("> ", ">").replace(" <", "<")
    assert "Valid" not in html
    assert "green" not in html


def test_a_readable_expiry_far_out_reads_as_valid() -> None:
    html = _render("Jan 01 00:00:00 2099 GMT")

    assert "Valid" in html
    assert "green" in html
