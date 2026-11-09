"""The page's Content-Security-Policy refuses inline script and inline style, and no
template needs either.

`script-src` is `'self' 'unsafe-eval'`: an inline `<script>`, an `on*=` handler
or a `javascript:` URL does not run, so an injected one is inert. `style-src` is
`'self'`: an injected `<style>` block or `style=` attribute is not applied, so
markup that slips through cannot restyle the page (hide a warning, overlay a
fake form). The same policy would silently break a template that still carried
one, so every web template is checked here as well. `'unsafe-eval'` stays for
Alpine.js and htmx `hx-on`. Email templates are out of scope: mail clients need
inline styles and the page's policy does not reach them.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from _paths import BACKEND_ROOT
from app.main import app
from httpx import ASGITransport, AsyncClient

TEMPLATES = BACKEND_ROOT / "templates"
WEB_TEMPLATES = TEMPLATES / "web"
# Vendored weather icons nothing includes; their `enable-background` is an SVG
# presentation hint no browser applies.
_UNUSED_VENDORED = WEB_TEMPLATES / "icons" / "weather"

# An HTML event-handler attribute (onclick=, onchange=, onsubmit=, ...).
_HANDLER = re.compile(r"""(?<![\w:-])on[a-z]+\s*=\s*["']""", re.IGNORECASE)
_JS_URL = re.compile(r"""(?:href|src|action)\s*=\s*["']\s*javascript:""", re.I)
# A style attribute (Alpine's `:style` / `x-bind:style` too, which a string value
# sets with setAttribute) or a style element in markup. Jinja is stripped first,
# so a macro's `style='primary'` argument is not markup.
_INLINE_STYLE = re.compile(r"""(?<![\w.-])style\s*=|<style[\s>]""", re.IGNORECASE)
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
# Inside a Jinja expression, markup arrives as a string literal: `'style="…"'`
# through `| safe` or `extra_attrs`, or a `'style'` key through `| xmlattr`.
_LITERAL = re.compile(r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*\"""", re.DOTALL)
_STYLE_KEY = re.compile(r"""\s*:""")


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _sources(policy: str, name: str) -> list[str]:
    for directive in policy.split(";"):
        directive_name, *sources = directive.split()
        if directive_name == name:
            return sources
    raise AssertionError(f"no {name} in {policy!r}")


def _markup(text: str) -> str:
    return _JINJA.sub("", text)


def _jinja_styles(text: str) -> list[str]:
    """Each string literal in a Jinja expression that writes a style: one holding
    a style attribute or element, or a `'style'` mapping key."""
    found = []
    for expression in _JINJA.finditer(text):
        source = expression.group(0)
        for literal in _LITERAL.finditer(source):
            body = literal.group(0)[1:-1]
            if (
                _INLINE_STYLE.search(body)
                or body.lower() == "style"
                and _STYLE_KEY.match(source, literal.end())
            ):
                found.append(literal.group(0))
    return found


def _inline_styles(text: str) -> list[str]:
    markup = [m.group(0) for m in _INLINE_STYLE.finditer(_markup(text))]
    return markup + _jinja_styles(text)


def _web_templates() -> list[Path]:
    return [
        path
        for path in sorted(WEB_TEMPLATES.rglob("*"))
        if path.suffix in (".html", ".svg") and _UNUSED_VENDORED not in path.parents
    ]


@pytest.mark.integration
async def test_the_policy_refuses_inline_script(client: AsyncClient) -> None:
    resp = await client.get("/health")

    sources = _sources(resp.headers["content-security-policy"], "script-src")
    assert sources == ["'self'", "'unsafe-eval'"]


@pytest.mark.integration
async def test_the_policy_refuses_inline_style(client: AsyncClient) -> None:
    resp = await client.get("/health")

    sources = _sources(resp.headers["content-security-policy"], "style-src")
    assert sources == ["'self'"]


@pytest.mark.unit
def test_no_template_carries_an_inline_handler_or_a_javascript_url() -> None:
    found = [
        f"{path.relative_to(TEMPLATES)}:{n}: {line.strip()}"
        for path in sorted(TEMPLATES.rglob("*.html"))
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if _HANDLER.search(line) or _JS_URL.search(line)
    ]

    assert found == []


@pytest.mark.unit
def test_no_web_template_carries_an_inline_style() -> None:
    found = [
        f"{path.relative_to(TEMPLATES)}: {style}"
        for path in _web_templates()
        for style in _inline_styles(path.read_text())
    ]

    assert found == []


@pytest.mark.unit
@pytest.mark.parametrize("layout", ["layouts/base.html", "layouts/public_status.html"])
def test_htmx_is_told_not_to_inject_its_indicator_style(layout: str) -> None:
    """htmx adds a `<style>` for its indicators unless configured not to; the
    policy refuses it, and input.css carries those rules instead."""
    text = (WEB_TEMPLATES / layout).read_text()

    assert re.search(
        r"""<meta name="htmx-config" content='\{[^}]*"includeIndicatorStyles":\s*false""",
        text,
    ), layout


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ('<div style="width: 2px">', True),
        ("<style>\n.x{}\n</style>", True),
        ("<stop offset='0' style='stop-color:red'/>", True),
        ("{{ text_btn(svg, 'Go', style='primary') }}", False),
        ("{% macro text_btn(label, style='primary') %}", False),
        ('<stop offset="0" stop-color="red"/>', False),
        ('<div class="x" data-style="y">', False),
        # Alpine sets a string binding with setAttribute('style', ...).
        ("""<div :style="'width: 40%'">""", True),
        ("""<div x-bind:style="'display: none'">""", True),
        # Markup a Jinja expression writes.
        ("""<div {{ 'style="color:red"' | safe }}>""", True),
        ("""<div {{ {'style': 'color:red'} | xmlattr }}>""", True),
        ("""{{ text_btn(svg, 'Go', extra_attrs='style="x"') }}""", True),
        ("""{{ text_btn(svg, 'Go', attrs={"style": "x"}) }}""", True),
        ("""{% if style == 'primary' %}""", False),
    ],
)
def test_the_scan_tells_inline_style_from_a_macro_argument(
    text: str, flagged: bool
) -> None:
    assert bool(_inline_styles(text)) is flagged


@pytest.mark.unit
@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ('<input onclick="x()">', True),
        ("extra_attrs='onchange=\"this.form.requestSubmit()\"'", True),
        ('<a href="javascript:void(0)">', True),
        ('<button hx-on::click="x()">', False),
        ('<div x-on:click="open = true">', False),
        ('<div data-autosubmit class="online">', False),
    ],
)
def test_the_scan_tells_a_handler_from_an_htmx_or_alpine_attribute(
    line: str, flagged: bool
) -> None:
    assert bool(_HANDLER.search(line) or _JS_URL.search(line)) is flagged
