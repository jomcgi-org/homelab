"""Tests for the recipe catalog manifest, including a guest-drift guard.

The drift guard reads the real, checked-in guest recipes at
projects/firecracker/goosecracker/guest/recipes/*.yaml and asserts the
manifest's id set exactly matches them (minus the non-selectable router
recipe, agent.yaml). Under Bazel this directory is shipped as test `data`
(see the BUILD entry for goosecracker_recipe_catalog_test); outside Bazel it
resolves against the real repo checkout.
"""

from __future__ import annotations

from pathlib import Path

from goosecracker import recipe_catalog

# This test file lives at projects/monolith/goosecracker/tests/, three
# directories below projects/: tests -> goosecracker -> monolith -> projects.
_GUEST_RECIPES = Path(__file__).parents[3] / "firecracker/goosecracker/guest/recipes"


def test_manifest_ids_and_paths() -> None:
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
        assert entry.baked_path == f"/home/goose-agent/recipes/{rid}.yaml"
        assert entry.description  # non-empty, used in the tool schema

    assert recipe_catalog.enabled_enum() == list(cat)


def test_manifest_matches_guest_recipes_on_disk() -> None:
    # Drift guard: the monolith manifest must track the baked guest recipes.
    # `agent` is the router itself, not a selectable sub-recipe.
    on_disk = {p.stem for p in _GUEST_RECIPES.glob("*.yaml")} - {"agent"}
    assert on_disk, f"expected guest recipe yaml files under {_GUEST_RECIPES}"
    assert set(recipe_catalog.CATALOG) == on_disk
