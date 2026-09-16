"""Flatten the app's route table, prefixes included.

FastAPI 0.141 stopped splicing an included router's children into
`app.routes`. Each `include_router()` now leaves ONE `_IncludedRouter` wrapper
behind, and the real routes hang off `.original_router.routes` — while the
prefix that used to be baked into each child's `.path` lives on the wrapper's
`.include_context.prefix`.

Two consequences, both of which bit this suite on the 0.136 -> 0.141 bump:

- Iterating `app.routes` flat sees ~29 entries instead of ~224. A check built
  on it does not fail; it silently stops checking.
- Reading `route.path` without the wrapper's prefix yields `/checks`, not
  `/api/v1/checks` — a path that matches nothing.

This lives in one place so the route-name test and the route-smoke sweep cannot
drift apart again. It mirrors the traversal in the emitted route-smoke asserter
(`tests/conftest.py`), which is owned by luxarch and must not be hand-edited.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def iter_routes(routes: Any, prefix: str = "") -> Iterator[tuple[Any, str]]:
    """Yield (route, full path) for every leaf route, depth-first."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ictx = getattr(route, "include_context", None)
            yield from iter_routes(
                inner.routes, prefix + (getattr(ictx, "prefix", "") or "")
            )
            continue

        sub = getattr(route, "routes", None)
        kind = type(route).__name__
        if sub is not None and kind == "Mount":
            # A sub-app carries its prefix on `.path`.
            yield from iter_routes(sub, prefix + (getattr(route, "path", "") or ""))
            continue
        if sub is not None and kind == "Host":
            # Discriminates on hostname, so it adds no path prefix.
            yield from iter_routes(sub, prefix)
            continue

        yield route, prefix + (getattr(route, "path", "") or "")
