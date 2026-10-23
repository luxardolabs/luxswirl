"""Shared code is importable only as `app.shared`, the apps/shared SSOT overlaid
on this leg's placeholder package, exactly as in the image. A top-level
`shared` package must not resolve here: an import of it would pass the suite
and fail in production, and would load a second copy of every shared module.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from app.shared import log_redaction

pytestmark = pytest.mark.unit


def test_the_top_level_shared_package_does_not_resolve() -> None:
    sys.modules.pop("shared", None)
    with pytest.raises(ImportError):
        importlib.import_module("shared")


def test_app_shared_is_the_real_code_on_the_placeholder() -> None:
    path = Path(log_redaction.__file__)

    assert path.parent.name == "shared"
    assert path.parent.parent.name == "app"
    assert path.stat().st_size > 0
