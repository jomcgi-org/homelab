"""Tests for the recipe catalog manifest and its YAML source files."""

from __future__ import annotations

from pathlib import Path

from goosecracker import recipe_catalog

_RECIPES = Path(__file__).parents[1] / "recipes"


def test_manifest_ids_and_descriptions() -> None:
    cat = recipe_catalog.CATALOG
    assert list(cat) == [
        "query",
        "research",
        "plan",
        "implement",
        "artifact-build",
        "artifact-review",
    ]
    for rid, entry in cat.items():
        assert entry.id == rid
        assert entry.description  # non-empty, used in the tool schema

    assert recipe_catalog.enabled_enum() == list(cat)


def test_manifest_matches_recipes_on_disk() -> None:
    on_disk = {p.stem for p in _RECIPES.glob("*.yaml")}
    assert on_disk, f"expected recipe yaml files under {_RECIPES}"
    assert set(recipe_catalog.CATALOG) == on_disk
