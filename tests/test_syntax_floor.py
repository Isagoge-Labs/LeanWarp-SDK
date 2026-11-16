from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCES = sorted((Path(__file__).parents[1] / "src" / "leanwarp_cloud").rglob("*.py"))


def test_the_package_has_sources_to_check() -> None:
    assert len(SOURCES) > 10


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_every_module_parses_at_the_oldest_supported_python(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))
