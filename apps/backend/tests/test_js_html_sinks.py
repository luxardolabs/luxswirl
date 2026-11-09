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


# Every value first-party JavaScript writes into HTML, each read and found to be a
# constant the code itself holds. A new one is a question to answer, not a name to
# add: build it with DOM calls unless it is a constant too.
_KNOWN_VALUES = {
    ("alerts-form.js", "defaults.consecutive_failures"),
    ("alerts-form.js", "defaults.latency_threshold_ms"),
    ("alerts-form.js", "help"),
    ("app.js", "icon"),
    ("app.js", "textColor"),
    ("registration-keys.js", "originalHTML"),
    ("settings.js", "originalHTML"),
    ("tag-pill.js", "REMOVE_ICON"),
}


def _values_written(text: str) -> list[str]:
    """What an HTML sink's right side takes from outside its own literals."""
    if text.startswith("`"):
        return [value.strip() for value in re.findall(r"\$\{([^}]*)\}", text)]
    without_literals = re.sub(r"'[^']*'|\"[^\"]*\"", "", text).strip()
    without_literals = without_literals.strip("+ ").strip()
    return [without_literals] if without_literals else []


@pytest.mark.parametrize(
    "source",
    [
        "x.innerHTML = `<b>${tag}</b>`;",
        "x.innerHTML = `<b>${t}</b>`;",
        "x.innerHTML = html;",
        "x.innerHTML = '<b>' + tag + '</b>';",
        "x.innerHTML = list.map(t => `${t}`).join('');",
        "x.insertAdjacentHTML('beforeend', html);",
    ],
)
def test_the_scan_sees_each_shape_of_a_value_written_as_html(source: str) -> None:
    assert any(_values_written(text) for text in _html_written(source)), source


def test_a_literal_written_as_html_is_not_a_value() -> None:
    written = _html_written("x.innerHTML = '<b>ok</b>';\ny.innerHTML = `<i>ok</i>`;")

    assert [_values_written(text) for text in written] == [[], []]


@pytest.mark.parametrize("path", _first_party(), ids=lambda p: p.name)
def test_html_sinks_write_only_known_constants(path: Path) -> None:
    for text in _html_written(path.read_text()):
        for value in _values_written(text):
            assert (path.name, value) in _KNOWN_VALUES, f"{path.name}: {value[:120]}"


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


def test_every_first_party_module_is_loaded_by_something() -> None:
    """A module nothing loads still ships and still reads as live: an HTML sink
    in it is a sink waiting for the template that wires it back up."""
    loaders = [p.read_text() for p in _TEMPLATES.rglob("*.html")]
    loaders += [p.read_text() for p in _APP.rglob("*.py")]
    sources = {p: p.read_text() for p in _first_party()}

    unloaded = [
        path.name
        for path in sources
        if not any(path.name in text for text in loaders)
        and not any(
            f"/{path.name}'" in text or f"./{path.name}'" in text
            for other, text in sources.items()
            if other != path
        )
    ]

    assert unloaded == []
