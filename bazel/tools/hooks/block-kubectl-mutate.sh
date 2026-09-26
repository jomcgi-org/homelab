#!/bin/bash
# PreToolUse hook: blocks kubectl and helm commands that write to the cluster.
#
# The cluster is GitOps: change projects/<service>/deploy/values.yaml, push,
# and ArgoCD syncs it. This is a Claude-side fast fail only; it cannot see
# other agents' shells. The parser is shared with the `kubectl-mutate` rule in
# bazel/tools/ci/source_ratchet.py, which catches writes committed into
# scripts for every author.
#
# Exit 0: allow; exit 2: block (reason on stderr).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RATCHET="${HERE}/../ci/source_ratchet.py"
if [[ ! -f "$RATCHET" && -n "${RUNFILES_DIR:-}" ]]; then
	RATCHET="${RUNFILES_DIR}/_main/bazel/tools/ci/source_ratchet.py"
fi

exec python3 "$RATCHET" --hook
