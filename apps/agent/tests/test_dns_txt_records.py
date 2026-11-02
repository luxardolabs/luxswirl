"""A TXT record is read as the nameserver sent it, whatever bytes it holds.

A TXT record longer than 255 bytes travels as several character-strings, and
the publisher's tools (SPF, DKIM) split it wherever the 255th byte falls, even
inside a multi-byte character. The check joins the strings as bytes, with
nothing between them, before decoding; a byte that is not UTF-8 is shown as
its escape (``\\xff``), and the check passes on the nameserver's answer.
"""

from __future__ import annotations

from uuid import uuid4

import dns.message
import dns.name
import dns.rdataclass
import dns.rdatatype
import dns.resolver
import pytest
from app.checks import dns as dns_mod
from app.checks.dns import DNSCheck
from dns.rdtypes.ANY.TXT import TXT

pytestmark = pytest.mark.unit

DOMAIN = "txt.example.test."


def _answer(*records: tuple[bytes, ...]) -> dns.resolver.Answer:
    """A resolver answer holding one TXT rdata per record, each its strings."""
    qname = dns.name.from_text(DOMAIN)
    query = dns.message.make_query(qname, dns.rdatatype.TXT)
    response = dns.message.make_response(query)
    rrset = response.find_rrset(
        response.answer, qname, dns.rdataclass.IN, dns.rdatatype.TXT, create=True
    )
    for strings in records:
        rrset.add(TXT(dns.rdataclass.IN, dns.rdatatype.TXT, strings), 300)
    return dns.resolver.Answer(qname, dns.rdatatype.TXT, dns.rdataclass.IN, response)


async def _run(monkeypatch, answer: dns.resolver.Answer, **config) -> dict:
    async def resolve(self, *args, **kwargs):
        return answer

    monkeypatch.setattr(dns_mod.dns.asyncresolver.Resolver, "resolve", resolve)
    check = DNSCheck(
        {
            "check_id": str(uuid4()),
            "name": "txt",
            "check_type": "dns",
            "target": DOMAIN,
            "nameserver": "127.0.0.1",
            "record_type": "TXT",
            "retries": 1,
            **config,
        }
    )
    return await check.run()


async def test_a_record_split_inside_a_character_is_read_whole(monkeypatch) -> None:
    value = "v=DKIM1; n=x" + "é" * 200
    raw = value.encode()
    assert 0x80 <= raw[255] < 0xC0  # the 255-byte split falls inside a character

    result = await _run(
        monkeypatch, _answer((raw[:255], raw[255:])), expect_value="é" * 150
    )

    assert result["success"] is True, result["error"]
    assert result["metrics"]["dns"]["records"] == [value]


async def test_the_strings_of_a_long_record_join_with_nothing_between(
    monkeypatch,
) -> None:
    key = "0123456789" * 40  # stands in for a public key: long, no spaces
    raw = f"v=DKIM1; k=rsa; p={key}".encode()

    result = await _run(
        monkeypatch, _answer((raw[:255], raw[255:])), expect_value=f"p={key}"
    )

    assert result["success"] is True, result["error"]
    assert result["metrics"]["dns"]["records"] == [raw.decode()]


async def test_a_byte_that_is_not_utf8_is_shown_escaped(monkeypatch) -> None:
    result = await _run(monkeypatch, _answer((b"token=\xff\xfeok",), (b"v=spf1 -all",)))

    assert result["success"] is True, result["error"]
    assert result["metrics"]["dns"]["records"] == ["token=\\xff\\xfeok", "v=spf1 -all"]
    assert result["metrics"]["dns"]["record_count"] == 2
