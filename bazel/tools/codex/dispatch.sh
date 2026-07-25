#!/usr/bin/env bash
# Deterministic wrapper for dispatching implementation work to the Codex CLI.
#
# Usage:
#   bazel/tools/codex/dispatch.sh <tier> <workdir> "<task spec>"
#   echo "<task spec>" | bazel/tools/codex/dispatch.sh <tier> <workdir> -
#
#   tier:    luna | terra | frontier
#            luna = default (most value / $); terra = step-up; frontier/Sol = rare
#   workdir: directory the worker may write to (a /tmp/claude-worktrees/* worktree)
#   spec:    full task spec as one argument, or "-" to read from stdin
#
# Exit codes:
#   0   worker finished
#   42  Codex quota / rate limit exhausted (caller must Discord-notify Joe once
#       and fall back to Sonnet subagents; see the codex-implement skill)
#   64  usage error
#   *   codex exec's own failure code, passed through
#
# Guardrails baked in:
#   - workspace-write sandbox scoped to <workdir>: no network, no writes
#     outside the worktree, so the worker cannot push or commit upstream
#   - repo guardrails appended to every spec (no local tests, no commits,
#     apko not Dockerfiles, non-root, conventional repo patterns)
set -euo pipefail

QUOTA_EXIT=42

usage() {
	echo "usage: $0 <luna|terra|frontier> <workdir> <spec|-> " >&2
	exit 64
}

[[ $# -eq 3 ]] || usage

case "$1" in
luna) MODEL="gpt-5.6-luna" EFFORT="medium" ;;
terra) MODEL="gpt-5.6-terra" EFFORT="high" ;;
frontier) MODEL="gpt-5.6-sol" EFFORT="high" ;;
*) usage ;;
esac

WORKDIR="$2"
[[ -d "$WORKDIR" ]] || {
	echo "workdir does not exist: $WORKDIR" >&2
	exit 64
}

if [[ "$3" == "-" ]]; then
	SPEC="$(cat)"
else
	SPEC="$3"
fi
[[ -n "$SPEC" ]] || {
	echo "empty task spec" >&2
	exit 64
}

GUARDRAILS='
--- Repo guardrails (do not violate) ---
- Do NOT run pytest/go test/npm test/bazel test on the Mac. The orchestrator
  runs `ci` (bb remote Linux Test) after reviewing your diff.
- Do NOT run git commit, git push, or any git state-changing command. The
  orchestrator reviews and commits your diff.
- Never use em-dashes in anything you write.
- Containers: apko only (no Dockerfiles), non-root uid 65532.
- Never hardcode .svc.cluster.local URLs or @sha256: image digests.
- When done, print a short summary of files changed and any open questions.'

LOG="$(mktemp -t codex-dispatch.XXXXXX.log)"
trap 'rm -f "$LOG"' EXIT

set +e
codex exec \
	--model "$MODEL" \
	--config model_reasoning_effort="$EFFORT" \
	--sandbox workspace-write \
	--skip-git-repo-check \
	-C "$WORKDIR" \
	"${SPEC}${GUARDRAILS}" 2>&1 | tee "$LOG"
CODE=${PIPESTATUS[0]}
set -e

# Quota / rate-limit detection: codex surfaces plan exhaustion as an error
# line rather than a distinct exit code, so grep the transcript. Gate on a
# non-zero exit so a task spec that merely mentions rate limits or 429s
# does not false-positive.
if [[ "$CODE" -ne 0 ]] &&
	grep -qiE 'usage limit|rate limit|too many requests|429|quota|plan limit' "$LOG"; then
	echo "CODEX_QUOTA_EXHAUSTED model=$MODEL" >&2
	exit "$QUOTA_EXIT"
fi

exit "$CODE"
