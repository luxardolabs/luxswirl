"""The page's Content-Security-Policy refuses inline script, and no template needs it.

`script-src` is `'self' 'unsafe-eval'`: an inline `<script>`, an `on*=` handler
or a `javascript:` URL does not run, so an injected one is inert. The same
policy would silently break a template that still carried one, so every
template is checked here as well. `'unsafe-eval'` stays for Alpine.js and htmx
`hx-on`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from _paths import BACKEND_ROOT
from app.main import app
from httpx import ASGITransport, AsyncClient

TEMPLATES = BACKEND_ROOT / "templates"

# An HTML event-handler attribute (onclick=, onchange=, onsubmit=, ...).
_HANDLER = re.compile(r"""(?<![\w:-])on[a-z]+\s*=\s*["']""", re.IGNORECASE)
_JS_URL = re.compile(r"""(?:href|src|action)\s*=\s*["']\s*javascript:""", re.I)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _script_src(policy: str) -> list[str]:
    for directive in policy.split(";"):
        name, *sources = directive.split()
        if name == "script-src":
            return sources
    raise AssertionError(f"no script-src in {policy!r}")


@pytest.mark.integration
async def test_the_policy_refuses_inline_script(client: AsyncClient) -> None:
    resp = await client.get("/health")

    sources = _script_src(resp.headers["content-security-policy"])
    assert sources == ["'self'", "'unsafe-eval'"]


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
