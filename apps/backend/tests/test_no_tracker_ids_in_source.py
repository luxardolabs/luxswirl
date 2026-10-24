"""Source and docs carry no internal tracker ids.

The repository is public and the tracker is not: an id in a comment points a
reader at nothing, and the reason for a change belongs in its commit message.
A released change log (app/change_logs/) is the record of what each release
fixed, so it is not scanned.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_TRACKER_ID = re.compile(r"\b(?:LUX)?SWIRL-\d+")
_SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".toml", ".yaml", ".yml"}
_SKIPPED_DIRS = {"node_modules", "vendor", "__pycache__", "change_logs"}


def tracker_ids(root: Path) -> list[str]:
    """Each `path:line: id` under root's apps/ and docs/, outside the skipped dirs."""
    found = []
    for top in ("apps", "docs"):
        for path in sorted((root / top).rglob("*")):
            if path.suffix not in _SUFFIXES or not path.is_file():
                continue
            if _SKIPPED_DIRS & set(path.relative_to(root).parts):
                continue
            for number, line in enumerate(
                path.read_text(errors="replace").splitlines(), 1
            ):
                for match in _TRACKER_ID.finditer(line):
                    found.append(f"{path.relative_to(root)}:{number}: {match.group()}")
    return found


def test_the_source_and_docs_name_no_tracker_id() -> None:
    assert tracker_ids(_REPO) == []


def test_a_tracker_id_in_a_comment_or_doc_is_found(tmp_path: Path) -> None:
    # Built from parts, so this file holds no id of its own.
    long_id, short_id, logged_id = (
        "LUXSWIRL" + "-12",
        "SWIRL" + "-57",
        "LUXSWIRL" + "-9",
    )
    (tmp_path / "apps" / "x").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "apps" / "x" / "a.py").write_text(f"x = 1  # see {long_id}\n")
    (tmp_path / "docs" / "b.md").write_text(f"Tracked in {short_id}.\n")
    (tmp_path / "apps" / "x" / "change_logs").mkdir()
    (tmp_path / "apps" / "x" / "change_logs" / "1.md").write_text(f"- {logged_id}\n")

    assert tracker_ids(tmp_path) == [
        f"apps/x/a.py:1: {long_id}",
        f"docs/b.md:1: {short_id}",
    ]
