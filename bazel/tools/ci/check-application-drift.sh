#!/usr/bin/env bash
# Diff the ArgoCD Applications this repository declares against the live objects.
#
# WHY THIS EXISTS
#
# ArgoCD reports success for drift it has been told to ignore, and one form of
# that instruction is broader than it looks. `RespectIgnoreDifferences=true`
# combined with an `ignoreDifferences` jsonPointer into `spec.sources` stops
# ArgoCD patching EVERY field in that array, not only the field named. The root
# keeps reporting "successfully synced (all tasks run)" while a source field sits
# permanently stale.
#
# That is not hypothetical. On 2026-08-26 all four Kargo-owned Applications were
# still pointing at the pre-org-move `github.com/jomcgi/homelab` repoURL while
# git said `jomcgi-org`. It had been that way since the org move. The 27
# Applications without `ignoreDifferences` healed on their own; the 4 with it did
# not, and the split mapped exactly onto that field. Nothing surfaced it: the
# only way it came to light was rendering the manifests and comparing by hand.
#
# This script is that comparison, made repeatable. It reports fields where git
# and the cluster disagree, EXCLUDING the ones ArgoCD is legitimately not
# expected to own (see IGNORED below), so what is left is either drift ArgoCD is
# about to heal, or drift ArgoCD has silently given up on.
#
# IT NEEDS A LIVE CLUSTER, so it is an operator command rather than a CI gate.
# CI runners hold no kubeconfig.
#
# USAGE
#
#   bazel/tools/ci/check-application-drift.sh [kube-context]
#
# Exits non-zero if any unexpected difference is found, so it can be wired into
# a routine job later without changing its contract.
set -euo pipefail

CONTEXT="${1:-$(kubectl config current-context)}"
CLUSTER_PATH="${CLUSTER_PATH:-projects/gke-cluster}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$repo_root"

rendered="$(mktemp -d)"
trap 'rm -rf "$rendered"' EXIT

echo "rendering ${CLUSTER_PATH} ..."
kubectl kustomize "$CLUSTER_PATH" >"$rendered/all.yaml"

# Fields the cluster legitimately owns or that carry no meaning for this check:
#
#   status                  runtime, never declared
#   metadata.{uid,resourceVersion,generation,creationTimestamp,managedFields}
#                           assigned by the API server
#   metadata.annotations    ArgoCD writes tracking-id and last-applied here
#   spec.sources[].targetRevision
#                           Kargo owns this at runtime by design (ADR
#                           platform/009), so a difference here is EXPECTED for a
#                           promoted app and is the one thing this check must not
#                           shout about. Everything else in `sources` is fair
#                           game, which is precisely the blind spot being closed.
python3 - "$rendered/all.yaml" "$CONTEXT" <<'PY'
import json
import os
import subprocess
import sys

import yaml

rendered_path, context = sys.argv[1], sys.argv[2]

declared = {}
self_managed = []
with open(rendered_path) as fh:
    for doc in yaml.safe_load_all(fh):
        if not doc or doc.get("kind") != "Application":
            continue
        declared[doc["metadata"]["name"]] = doc


def resolve(name, doc):
    """Follow a self-managing wrapper to the definition that actually wins.

    Several platform apps are declared twice on purpose: a thin wrapper here
    points at a directory, and that directory contains the real Application of
    the SAME name. ArgoCD applies the wrapper, the wrapper syncs the directory,
    and the nested spec overwrites the wrapper's. The live object therefore
    matches the nested file, never the wrapper, and comparing against the
    wrapper would report drift on every one of them forever.
    """
    src = doc["spec"].get("source") or {}
    path = src.get("path")
    if not path:
        return doc
    # ONLY when the wrapper syncs the directory as plain manifests. A wrapper
    # carrying a `helm:` block renders that directory as a CHART, so an
    # application.yaml sitting in it is just a file in the chart source (it is
    # the home cluster's definition of the same app) and is never applied here.
    # Following it would compare GKE against the home cluster's config and
    # report drift on every shared platform app, which is exactly the wrong
    # answer: the wrapper is authoritative on this cluster.
    if src.get("helm"):
        return doc
    nested_path = os.path.join(path, "application.yaml")
    if not os.path.exists(nested_path):
        return doc
    with open(nested_path) as fh:
        for nested in yaml.safe_load_all(fh):
            if (
                nested
                and nested.get("kind") == "Application"
                and nested.get("metadata", {}).get("name") == name
            ):
                self_managed.append(f"{name} (via {nested_path})")
                return nested
    return doc


declared = {name: resolve(name, doc) for name, doc in declared.items()}


def strip(spec):
    """Drop the fields the cluster owns, so what remains is what git asserts."""
    spec = json.loads(json.dumps(spec))
    for source in spec.get("sources", []) or []:
        source.pop("targetRevision", None)
    if "source" in spec:
        spec["source"].pop("targetRevision", None)
    return spec


live_raw = subprocess.run(
    ["kubectl", "--context", context, "get", "applications", "-n", "argocd", "-o", "json"],
    check=True,
    capture_output=True,
    text=True,
).stdout
live = {a["metadata"]["name"]: a for a in json.loads(live_raw)["items"]}

problems = []
for name, doc in sorted(declared.items()):
    if name not in live:
        problems.append(f"{name}: declared in git, ABSENT from the cluster")
        continue
    want, got = strip(doc["spec"]), strip(live[name]["spec"])
    if want != got:
        for key in sorted(set(want) | set(got)):
            if want.get(key) != got.get(key):
                problems.append(
                    f"{name}.spec.{key}:\n"
                    f"      git:  {json.dumps(want.get(key), sort_keys=True)}\n"
                    f"      live: {json.dumps(got.get(key), sort_keys=True)}"
                )

print(f"compared {len(declared)} declared Application(s) against {len(live)} live")
if self_managed:
    print(
        f"resolved {len(self_managed)} self-managing wrapper(s) to their nested definition:"
    )
    for entry in sorted(self_managed):
        print(f"  - {entry}")
if not problems:
    print("no drift outside targetRevision")
    sys.exit(0)

print("\nDRIFT (git says one thing, the cluster another):\n")
for p in problems:
    print(f"  - {p}")
print(
    "\nIf ArgoCD reports these Applications as Synced, it has stopped healing "
    "these fields. Check whether the Application carries an ignoreDifferences "
    "entry pointing into spec.sources alongside RespectIgnoreDifferences: that "
    "combination blocks patching the WHOLE sources array, not just the field "
    "named. The fix is a one-time kubectl patch of the live object, because git "
    "is already correct."
)
sys.exit(1)
PY
