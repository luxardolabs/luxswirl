"""Reusable id-typing guard — fleet-shared engine.

Flags params/fields/returns named like a UUID id but typed ``int``/``str``
(``int`` silently never matches a UUID; ``str`` forces str<->UUID reconversion at
every hop instead of carrying the UUID the column stores).

This module is **ORM-agnostic and host-agnostic**: it imports neither the host
app nor any specific ORM. A per-repo caller supplies three things:

* the set of UUID id-names — produced by an *adapter* (e.g.
  :func:`sqlalchemy_uuid_id_names` for SQLAlchemy ``Mapped[UUID]`` models; write
  a different adapter for Django/SQLModel/Prisma/etc.),
* the files to scan + their root, and
* a :class:`GuardConfig` (allowlist, prefix policy, extra aliases).

The engine then resolves names with prefix-stripping and configurable aliases so
``new_agent_id`` / ``source_check_id`` / ``log_id`` are caught, not just literal
columns. Distribute this file as a versioned dependency across the fleet so a fix
here propagates everywhere and no platform can quietly weaken it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# Default prefixes stripped before matching ``<prefix><uuid_col>``. Catches the
# common id-bearing-param family (new_agent_id, source_check_id, target_agent_id)
# using only the known UUID column set — no fuzzy entity-name guessing.
DEFAULT_STRIP_PREFIXES: tuple[str, ...] = (
    "new_",
    "old_",
    "source_",
    "target_",
    "parent_",
    "original_",
    "from_",
    "to_",
)


@dataclass(frozen=True)
class Violation:
    """A single id typed int/str where a UUID was expected."""

    path: str
    lineno: int
    name: str
    actual: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: '{self.name}' typed {self.actual}, expected UUID"


@dataclass(frozen=True)
class GuardConfig:
    """Per-repo policy. Everything here is data, not code — so the same engine
    serves every platform with a different config block."""

    bad_types: frozenset[str] = frozenset({"int", "str"})
    strip_prefixes: tuple[str, ...] = DEFAULT_STRIP_PREFIXES
    match_plural: bool = True
    # Names that reference a UUID pk but are not literal columns (e.g. ``log_id``
    # -> NotificationLog.id). Bridges the entity-alias gap a column scan can't see.
    extra_uuid_id_names: frozenset[str] = frozenset()
    # (relative_path, name) pairs that genuinely hold a non-UUID value. Keep this
    # draining to zero — never grandfather debt into it.
    allowlist: frozenset[tuple[str, str]] = frozenset()


def annotation_element_type(node: ast.expr) -> str | None:
    """Innermost type name of an annotation, unwrapping
    Mapped/Annotated/Optional/list/Sequence/set and ``X | None``."""
    if isinstance(node, ast.Subscript):
        base = node.value
        base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
        sl = node.slice
        inner = sl.elts[0] if isinstance(sl, ast.Tuple) else sl
        if base_name in {"Mapped", "Annotated", "Optional", "list", "Sequence", "set"}:
            return annotation_element_type(inner)
        return base_name
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):  # X | None
        for side in (node.left, node.right):
            t = annotation_element_type(side)
            if t and t != "None":
                return t
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):  # uuid.UUID -> "UUID"
        return node.attr
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    return None


def is_uuid_id_name(name: str, uuid_names: frozenset[str], cfg: GuardConfig) -> bool:
    """Does ``name`` denote a UUID id — directly, pluralised, prefix-stripped, or
    via a configured alias?"""
    candidates = {name}
    if cfg.match_plural and name.endswith("s"):
        candidates.add(name[:-1])
    for prefix in cfg.strip_prefixes:
        if name.startswith(prefix):
            stem = name[len(prefix) :]
            candidates.add(stem)
            if cfg.match_plural and stem.endswith("s"):
                candidates.add(stem[:-1])
    known = uuid_names | cfg.extra_uuid_id_names
    return bool(candidates & known)


def _annotated_sites(tree: ast.AST) -> Iterator[tuple[str, ast.expr | None, int]]:
    """Yield (name, annotation, lineno) for every annotated param and annotated
    field.

    NOTE: return-type checking (e.g. ``get_alert_ids_for_check -> set[str]``) is a
    planned layer — it needs token-subsequence matching against the function name
    rather than the whole-name match used for params, so it is intentionally not
    wired here yet to avoid false positives.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
                yield arg.arg, arg.annotation, arg.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            yield node.target.id, node.annotation, node.lineno


def find_violations(
    files: Iterable[Path],
    root: Path,
    uuid_names: frozenset[str],
    cfg: GuardConfig,
) -> list[Violation]:
    """Scan ``files`` and return every id typed in ``cfg.bad_types`` that should
    be UUID. ``root`` makes the reported path repo-relative for the allowlist."""
    offenders: list[Violation] = []
    for path in files:
        rel = str(path.relative_to(root))
        tree = ast.parse(path.read_text())
        for name, annotation, lineno in _annotated_sites(tree):
            if annotation is None or not is_uuid_id_name(name, uuid_names, cfg):
                continue
            element = annotation_element_type(annotation)
            if element in cfg.bad_types and (rel, name) not in cfg.allowlist:
                offenders.append(Violation(rel, lineno, name, element or "?"))
    return offenders


# --------------------------------------------------------------------------- #
# Adapters: produce ``uuid_names`` facts from a given ORM. One per stack.       #
# --------------------------------------------------------------------------- #


def sqlalchemy_uuid_id_names(models_files: Iterable[Path]) -> frozenset[str]:
    """SQLAlchemy adapter: column names that are ``Mapped[UUID]`` across every
    model and *nothing else* (a name typed UUID in one model and str in another
    is ambiguous and excluded). Swap this function to retarget another ORM."""
    by_name: dict[str, set[str]] = {}
    for path in models_files:
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and isinstance(node.annotation, ast.Subscript)
                and isinstance(node.annotation.value, ast.Name)
                and node.annotation.value.id == "Mapped"
            ):
                inner = annotation_element_type(node.annotation)
                if inner:
                    by_name.setdefault(node.target.id, set()).add(inner)
    return frozenset(name for name, types in by_name.items() if types == {"UUID"})
