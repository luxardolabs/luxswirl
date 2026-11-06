"""First-party JavaScript writes no tag into an HTML string, and a file loaded as a
classic script has no `import`.

A tag comes from a stored value or from the URL (`?tags=` on a status page's manage
page). Parsed as HTML, it can add Alpine's `x-init`, which the CSP's 'unsafe-eval'
lets run, so a crafted link would run code as whoever opens it. Tags go through DOM
calls (`tag-pill.js`). The behaviour itself needs a browser; these hold the shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_APP = Path(__file__).resolve().parents[1] / "app"
_JS = _APP / "static" / "js"
_TEMPLATES = _APP / "templates" / "web"

_SINK = re.compile(r"(?:innerHTML|outerHTML)\s*\+?=\s*|insertAdjacentHTML\([^,]+,\s*")
_CLASSIC_SCRIPT = re.compile(
    r"<script(?![^>]*type=[\"']module[\"'])[^>]*src=[\"']/static/js/([\w./-]+?\.js)"
)


def _first_party() -> list[Path]:
    return [p for p in _JS.rglob("*.js") if "vendor" not in p.parts]


def _html_written(source: str) -> list[str]:
    """Each string or template literal written into an HTML sink, up to its statement's end."""
    written = []
    for match in _SINK.finditer(source):
        tail = source[match.end() :]
        if tail.startswith("`"):
            written.append(tail[: tail.index("`", 1) + 1])
        else:
            written.append(tail.split(";", 1)[0])
    return written


def test_the_sink_finder_sees_the_shapes_it_must_refuse() -> None:
    found = _html_written(
        "pill.innerHTML = `<b>${tag}</b>`;\nx.innerHTML = list.map(tag => `${tag}`);"
    )

    assert any("tag" in text for text in found)
    assert len(found) == 2


@pytest.mark.parametrize("path", _first_party(), ids=lambda p: p.name)
def test_no_tag_reaches_an_html_sink(path: Path) -> None:
    for text in _html_written(path.read_text()):
        assert not re.search(r"\$\{[^}]*\btags?\b", text), f"{path.name}: {text[:120]}"
        assert not re.search(r"\.map\(\s*tags?\b", text), f"{path.name}: {text[:120]}"


def test_a_file_loaded_as_a_classic_script_has_no_import() -> None:
    """An `import` statement is a syntax error there, which kills the whole file."""
    classic = {
        name
        for template in _TEMPLATES.rglob("*.html")
        for name in _CLASSIC_SCRIPT.findall(template.read_text())
        if "vendor" not in name
    }

    assert classic, (
        "the scan found no classic script, so it is not looking at the right tags"
    )
    for name in sorted(classic):
        source = (_JS / name).read_text()
        assert not re.search(r"^\s*import\s", source, re.M), name
