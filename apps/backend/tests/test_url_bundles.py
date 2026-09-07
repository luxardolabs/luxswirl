"""URL bundles must not hide a key behind a dict method.

Templates read a bundle by attribute (`{{ agent_urls.update }}`). Jinja's
getattr wins before the item lookup, so on a plain dict `.update` resolves to
`dict.update` — the bound METHOD — and the page renders
`hx-post="<built-in method update of dict object at 0x…>"` with no error
anywhere. Real bug, found in the rendered check-edit form.

The fix is a frozen dataclass per bundle, so this test fails any dict literal
that both carries url_for values and uses a key that `dict` already defines.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_APP = pathlib.Path(__file__).resolve().parents[1] / "app"
# View services build the bundles; app/core holds the pure shapers some of them
# delegate to, so both are scanned.
SCANNED = sorted(
    [*(_APP / "services" / "views").glob("*.py"), *(_APP / "core").glob("*.py")]
)
DICT_ATTRS = frozenset(dir(dict))


def _is_url_call(node: ast.expr) -> bool:
    """True if the expression looks like `request.url_for(...)…` or `u(...)`."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute) and func.attr == "url_for":
            return True
        if isinstance(func, ast.Name) and func.id == "u":
            return True
    return False


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.name)
def test_no_url_bundle_key_shadows_a_dict_method(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            k.value
            for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
        shadowed = [k for k in keys if k in DICT_ATTRS]
        if not shadowed:
            continue
        if any(v is not None and _is_url_call(v) for v in node.values):
            offenders.append((node.lineno, shadowed))

    assert not offenders, (
        f"{path.name}: URL bundle keys shadow a dict method {offenders} — make the "
        "bundle a frozen dataclass so the template's attribute read resolves to "
        "the URL, not to dict's method"
    )
