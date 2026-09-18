"""Semantic clone clustering shared by recall and the offline gardener."""

from __future__ import annotations

import math

# High similarity is intentional: related facts are not necessarily clones.
CLONE_COSINE_THRESHOLD = 0.95


def cosine(left, right) -> float:
    if left is None or right is None or len(left) != len(right) or not len(left):
        return -1.0
    norm = math.sqrt(
        sum(float(x) ** 2 for x in left) * sum(float(x) ** 2 for x in right)
    )
    if not norm or not math.isfinite(norm):
        return -1.0
    return sum(float(x) * float(y) for x, y in zip(left, right)) / norm


def _are_clones(left: dict, right: dict) -> bool:
    left_vectors = left.get("embeddings", [left.get("embedding")])
    right_vectors = right.get("embeddings", [right.get("embedding")])
    if not left_vectors or not right_vectors:
        return False
    # A store merge must cover the whole fact in both directions. Sharing one
    # paragraph is enough to dedupe a recall snippet, but not to invalidate a
    # longer fact that carries additional information.
    matches = [
        [cosine(a, b) >= CLONE_COSINE_THRESHOLD for b in right_vectors]
        for a in left_vectors
    ]
    return all(any(row) for row in matches) and all(
        any(column) for column in zip(*matches)
    )


def clone_clusters(items: list[dict]) -> list[list[dict]]:
    """Connected components, with deterministic, verified-first survivors."""
    parents = list(range(len(items)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, item in enumerate(items):
        for j in range(i):
            other = items[j]
            if item.get("scope") != other.get("scope"):
                continue
            if _are_clones(item, other):
                parents[root(i)] = root(j)
    groups: dict[int, list[dict]] = {}
    for i, item in enumerate(items):
        groups.setdefault(root(i), []).append(item)
    for group in groups.values():
        group.sort(
            key=lambda item: (
                not (
                    item.get("verification_state") == "verified"
                    and not item.get("disputed")
                ),
                -float(item.get("confidence") or 0.0),
                str(item["note_id"]),
            )
        )
    return list(groups.values())


def dedupe(items: list[dict]) -> list[dict]:
    groups = clone_clusters(items)
    # Keep cluster relevance even when its strongest verified fact was ranked lower.
    groups.sort(
        key=lambda group: max(float(item.get("score") or 0) for item in group),
        reverse=True,
    )
    return [group[0] for group in groups]
