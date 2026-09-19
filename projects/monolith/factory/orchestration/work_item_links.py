"""Parse and reconcile work item block edges from GitHub issue bodies."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlmodel import Session, select

from factory.orchestration.factory_models import WorkItem, WorkItemEdge
from factory.orchestration.work_items import (
    WorkItemError,
    add_edge,
    remove_edge,
)

logger = logging.getLogger(__name__)

_PHRASE = re.compile(
    r"\b(?P<phrase>blocked\s+by|depends\s+on|waits\s+on|blocks)\b",
    re.IGNORECASE,
)
_REFERENCE = re.compile(r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<number>\d+)")
_SEPARATOR = re.compile(r"(?:\s|,|&|\band\b|\balso\b)+", re.IGNORECASE)


def parse_body_links(body: str, repo: str) -> dict[str, set[int]]:
    """Extract block relationships from an issue body.

    Returns {"blocked_by": {...}, "blocks": {...}} with issue numbers.

    Case-insensitive phrases:
    - "blocked by #N", "depends on #N", "waits on #N" (N blocks this issue)
    - "blocks #N" (this issue blocks N)

    Accepts #N and owner/repo#N for the same repo only. Ignores matches
    inside fenced code blocks and bare #N without a phrase.
    """
    result: dict[str, set[int]] = {"blocked_by": set(), "blocks": set()}

    if not isinstance(body, str) or not body.strip():
        return result

    # Remove fenced code blocks
    cleaned = re.sub(r"```[\s\S]*?```", "", body)

    for phrase_match in _PHRASE.finditer(cleaned):
        phrase = phrase_match.group("phrase").lower()
        kind = "blocks" if phrase == "blocks" else "blocked_by"
        position = phrase_match.end()
        while True:
            separator = _SEPARATOR.match(cleaned, position)
            if separator is None:
                break
            reference = _REFERENCE.match(cleaned, separator.end())
            if reference is None:
                break
            reference_repo = reference.group("repo")
            if reference_repo is None or reference_repo.lower() == repo.lower():
                result[kind].add(int(reference.group("number")))
            position = reference.end()

    return result


def reconcile_body_edges(
    db: Session,
    repo: str,
    items: list[tuple[WorkItem, str]],
    *,
    actor: str,
    truncated: bool = False,
) -> dict[str, Any]:
    """Reconcile github_body edges from issue bodies.

    For github-authority items only, compute desired edges from body text,
    compare with existing edges, add missing ones, and remove stale ones.
    Never touches edges with other sources. Counts cycles and skips them.

    Args:
        db: Database session
        repo: Repository identifier
        items: List of (WorkItem, body_text) tuples
        actor: Actor performing the reconciliation

    Returns:
        Dict with reconciliation counts, including removals skipped when the
        GitHub listing is truncated.
    """
    counts: dict[str, int] = {
        "added": 0,
        "removed": 0,
        "removal_skipped_truncated": 0,
        "cycles": 0,
        "skipped_local": 0,
    }
    skipped_removals: set[int] = set()
    attempted_additions: set[tuple[int, int]] = set()

    # Build a map of issue number to work item for this repo
    item_by_number: dict[int, WorkItem] = {}
    for item in [i[0] for i in items]:
        if (
            item.github_repo == repo
            and item.github_issue_number is not None
            and item.authority == "github"
        ):
            item_by_number[item.github_issue_number] = item

    # Collect all desired edges from all items
    # Each edge is from_id -> to_id
    all_desired_edges: dict[tuple[int, int], str] = {}

    for work_item, body in items:
        # Only process github-authority items
        if work_item.authority != "github" or work_item.id is None:
            continue

        # Parse the body for links
        links = parse_body_links(body, repo)

        # "blocked_by" means this item is blocked by other items
        # This creates edges FROM those items TO this item
        for other_number in links["blocked_by"]:
            other_item = item_by_number.get(other_number)
            if other_item is not None and other_item.id is not None:
                # other_item blocks work_item
                all_desired_edges[(other_item.id, work_item.id)] = "blocks"

        # "blocks" means this item blocks other items
        # This creates edges FROM this item TO those items
        for other_number in links["blocks"]:
            other_item = item_by_number.get(other_number)
            if other_item is not None and other_item.id is not None:
                # work_item blocks other_item
                all_desired_edges[(work_item.id, other_item.id)] = "blocks"

    # Now reconcile for each item
    for work_item, body in items:
        # Only process github-authority items
        if work_item.authority != "github" or work_item.id is None:
            if work_item.authority == "local":
                counts["skipped_local"] += 1
            continue

        # Get desired edges for this item (as from_id or to_id)
        item_desired_edges = {
            edge_key: kind
            for edge_key, kind in all_desired_edges.items()
            if edge_key[0] == work_item.id or edge_key[1] == work_item.id
        }

        # Get existing github_body edges for this item
        existing = db.exec(
            select(WorkItemEdge).where(
                (WorkItemEdge.from_id == work_item.id)
                | (WorkItemEdge.to_id == work_item.id),
                WorkItemEdge.source == "github_body",
                WorkItemEdge.kind == "blocks",
            )
        ).all()

        existing_edges: dict[tuple[int, int], WorkItemEdge] = {}
        for edge in existing:
            if edge.id is not None:
                existing_edges[(edge.from_id, edge.to_id)] = edge

        # Add missing edges
        for (from_id, to_id), kind in item_desired_edges.items():
            edge_key = (from_id, to_id)
            if edge_key not in existing_edges and edge_key not in attempted_additions:
                attempted_additions.add(edge_key)
                try:
                    add_edge(
                        db,
                        from_id,
                        to_id,
                        kind,
                        actor=actor,
                        author_kind="system",
                        cause_kind="github_body",
                        cause_ref=work_item.source_ref,
                        source="github_body",
                    )
                    counts["added"] += 1
                except WorkItemError as exc:
                    if "cycle" in str(exc).lower():
                        counts["cycles"] += 1
                        from factory.orchestration import factory_intake_loop

                        factory_intake_loop._throttled(
                            "work_item_edge_cycle",
                            {
                                "repo": repo,
                                "from_id": from_id,
                                "to_id": to_id,
                            },
                            session=db,
                        )
                    else:
                        raise

        # Remove stale edges (edges that exist but are not desired)
        for (from_id, to_id), edge in existing_edges.items():
            if (from_id, to_id) not in item_desired_edges:
                if edge.id is not None:
                    if truncated:
                        if edge.id not in skipped_removals:
                            skipped_removals.add(edge.id)
                            counts["removal_skipped_truncated"] += 1
                        continue
                    try:
                        remove_edge(
                            db,
                            from_id,
                            to_id,
                            "blocks",
                            actor=actor,
                            author_kind="system",
                            cause_kind="github_body",
                            cause_ref=work_item.source_ref,
                        )
                        counts["removed"] += 1
                    except WorkItemError:
                        pass

    return counts
