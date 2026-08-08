"""Recipe catalog for selectable agent routines.

This module is the one place that knows the sub-recipe id set, the human
descriptions surfaced in the orchestrator's ``submit_plan`` tool schema, and
their descriptions. The schema enum derives from ``CATALOG`` rather than
repeating the id set. A drift test keeps this manifest aligned with the recipe
YAML files in ``goosecracker/recipes``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecipeEntry:
    """One selectable sub-recipe and its tool-schema description."""

    id: str
    description: str


def _entry(recipe_id: str, description: str) -> RecipeEntry:
    return RecipeEntry(id=recipe_id, description=description)


# Ordered: this order is preserved in the tool schema enum. Keep it stable
# rather than alphabetizing, it roughly follows the query -> plan -> implement
# -> artifact lifecycle a session tends to move through.
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
