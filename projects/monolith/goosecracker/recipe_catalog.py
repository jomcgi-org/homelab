"""Recipe catalog: single source of truth for selectable sub-recipes.

This module is the one place that knows the sub-recipe id set, the human
descriptions surfaced in the DeepSeek orchestrator's ``submit_plan`` tool
schema, and the baked guest recipe paths those ids resolve to. Everything
downstream (the tool schema enum, the router renderer's ``sub_recipes``
list) derives from ``CATALOG`` rather than repeating the id set.

The router recipe itself (``agent.yaml``) is the classifier the runtime
router replaces; it is not a selectable sub-recipe and is intentionally
absent here. A drift-guard test (``goosecracker/tests/recipe_catalog_test.py``)
asserts this manifest's id set matches the checked-in guest recipes at
``projects/firecracker/goosecracker/guest/recipes/*.yaml`` minus ``agent``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecipeEntry:
    """One selectable sub-recipe: its id, tool-schema description, and baked guest path."""

    id: str
    description: str
    baked_path: str


def _entry(recipe_id: str, description: str) -> RecipeEntry:
    return RecipeEntry(
        id=recipe_id,
        description=description,
        baked_path=f"/home/goose-agent/recipes/{recipe_id}.yaml",
    )


# Ordered: this order is preserved in the tool schema enum and, by default,
# in the router's rendered sub_recipes list. Keep it stable rather than
# alphabetizing, it roughly follows the query -> plan -> implement ->
# artifact lifecycle a session tends to move through.
CATALOG: dict[str, RecipeEntry] = {
    "query": _entry(
        "query",
        "Read-only investigation: answer a question about the repo or "
        "cluster without changing anything.",
    ),
    "research": _entry(
        "research",
        "Web research: search and read public sources to answer a "
        "question, confirm an assumption, or gather current external "
        "facts. Read-only.",
    ),
    "plan": _entry(
        "plan",
        "Planning: turn a feature or change request into a written "
        "implementation plan, without implementing it.",
    ),
    "implement": _entry(
        "implement",
        "Implementation: make a code or config change, commit it, and open a PR.",
    ),
    "artifact-build": _entry(
        "artifact-build",
        "Build a single self-contained web artifact (may use CDN libs "
        "and live https APIs); the harness publishes it to a live URL.",
    ),
    "artifact-review": _entry(
        "artifact-review",
        "Review and polish an already-built web artifact in isolated, "
        "fresh-eyes context: fix real correctness and design issues in "
        "place without rewriting what already works.",
    ),
}


def enabled_enum() -> list[str]:
    """Return the ordered list of selectable sub-recipe ids, for the submit_plan tool schema."""
    return list(CATALOG)
