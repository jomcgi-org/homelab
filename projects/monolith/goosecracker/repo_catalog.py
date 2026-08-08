"""Repo catalog: descriptions of the hydratable repos, for repo selection.

The orchestrator picks the agent brief's ``repo`` from the
invoker's ADR 029 grants. Names alone (jomcgi/homelab, weave-hand/loom) are a
weak signal, so this module carries a one-line description per registered repo
and renders the invoker-scoped menu that the route prompt injects into its user
message. It rides in the user message (never the byte-stable system bundle)
because the granted set is per-invoker: a scope-specific list in the cached
system prefix would break the provider prefix cache.

The ids are the allowlist used by agent-session creation. A repo in a grant but
not here still appears in the orchestrator menu with a generic description, but
session creation rejects it until an entry is added. Seed descriptions are
hand-written; a future routine may regenerate them from each repo's
README/CLAUDE.md.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class RepoEntry:
    """One hydratable repo: its owner/repo id and a one-line selection description."""

    id: str
    description: str


# Ordered owner/repo -> description. The order is preserved in the rendered menu
# for the repos an invoker holds; keep it stable rather than alphabetizing so
# the owner's two primary repos lead.
REPO_CATALOG: dict[str, RepoEntry] = {
    "jomcgi/homelab": RepoEntry(
        "jomcgi/homelab",
        "This secure Kubernetes homelab: every service, operator, and website "
        "colocated with its GitOps deploy config (Helm + ArgoCD), built with "
        "Bazel + apko. Go, Python, JavaScript, Starlark. Choose this for the "
        "cluster, its apps and websites, CI, or infrastructure work.",
    ),
    "weave-hand/loom": RepoEntry(
        "weave-hand/loom",
        "Loom: a pre-alpha open-source typed-object data platform replicating "
        "Palantir Foundry's core concepts with a ports-and-adapters "
        "architecture. Private, under the weave-hand org. Choose this for Loom "
        "platform, data-object, or ontology work.",
    ),
    "colincee/homelab": RepoEntry(
        "colincee/homelab",
        "A collaborator's own homelab fork (ColinCee): their personal "
        "Kubernetes/infra setup, not this cluster. Choose this only when the "
        "task is explicitly about ColinCee's repo.",
    ),
    "scotscottmca/parkedlikea": RepoEntry(
        "scotscottmca/parkedlikea",
        "A collaborator's project (scotscottmca/parkedlikea). Choose this only "
        "when the task explicitly names it.",
    ),
}


def describe_repos(scopes: Iterable[str]) -> str:
    """Render the repo-selection menu for an invoker's granted ``scopes``.

    Lists the granted repos as ``- <owner/repo> = <description>`` lines: catalog
    order first for known repos the invoker holds, then any granted-but-
    uncatalogued repo (sorted) with a generic description, so a grant is never
    hidden. Deterministic for a given scope set. Returns a "(none)" sentinel
    when the invoker holds no repo grants.
    """
    scope_set = set(scopes)
    known = [entry for entry in REPO_CATALOG.values() if entry.id in scope_set]
    extra = sorted(scope_set - set(REPO_CATALOG))
    if not known and not extra:
        return "(none: the invoker holds no repo grants, so no repo may be named)"
    lines = [f"- {entry.id} = {entry.description}" for entry in known]
    lines += [f"- {rid} = (no description on file)" for rid in extra]
    return "\n".join(lines)
