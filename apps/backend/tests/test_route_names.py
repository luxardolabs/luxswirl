"""Route names must be unique across the whole app.

`url_path_for(name)` resolves to the FIRST route registered under that name, so
a web page and an API endpoint sharing one would send a link to the wrong
endpoint without an error. Every route names itself `[area.]<resource>.<action>`:
the web page and its API endpoint are two names (`checks.update`,
`api.admin.checks.update`).
"""

from __future__ import annotations

import pathlib
import re
from collections import defaultdict

from _route_table import iter_routes
from app.main import app


def test_route_names_are_unique() -> None:
    by_name: defaultdict[str, set[str]] = defaultdict(set)
    for route, path in iter_routes(app.routes):
        name = getattr(route, "name", None)
        if name and path:
            by_name[name].add(path)

    collisions = {
        name: sorted(paths) for name, paths in by_name.items() if len(paths) > 1
    }
    assert not collisions, (
        "route names resolve ambiguously in url_for() — give the web route an "
        f"explicit unique name=: {collisions}"
    )


def test_every_referenced_route_name_exists() -> None:
    """A url_for()/url_path_for() name that no route defines only fails when
    that page renders.

    Several of these live on partials that a test suite never opens, so catch a
    rename here instead of in production.
    """
    known = {
        name for r, _ in iter_routes(app.routes) if (name := getattr(r, "name", None))
    }
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(r'(?:url_for|url_path_for|path_for|\bu)\(\s*"([a-z_.]+)"')

    missing: dict[str, list[str]] = {}
    for path in app_dir.rglob("*.py"):
        for name in sorted(set(pattern.findall(path.read_text()))):
            if name not in known:
                missing.setdefault(str(path.relative_to(app_dir)), []).append(name)

    assert not missing, f"url_for() names with no matching route: {missing}"
