#!/usr/bin/env bash
# Deterministic wrapper for dispatching implementation work to the Codex CLI.
#
# Usage:
#   "$WORKDIR"/bazel/tools/codex/dispatch.sh <tier> "$WORKDIR" "<task spec>"
#   echo "<task spec>" | "$WORKDIR"/bazel/tools/codex/dispatch.sh <tier> "$WORKDIR" -
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
#   65  another dispatch already owns this workdir. Never double-dispatch a
#       worktree: two workers interleave edits. Wait for the running worker
#       (its live log path is printed) or kill it explicitly first.
#   *   codex exec's own failure code, passed through
#
# Runs take minutes to over an hour (median ~2.5 min, p90 ~8 min, tail 80+
# min). Callers MUST invoke this in the background (run_in_background), never
# in a foreground shell whose default timeout kills the worker mid-run and
# leaves a partial diff that reads as a wrong implementation at review.
#
# Guardrails baked in:
#   - workspace-write sandbox scoped to <workdir>: no writes outside the
#     worktree
#   - network IS enabled in the sandbox. workspace-write denies it by default,
#     which silently broke any worker that needed to fetch a dependency or read
#     an upstream doc: the turn still connects and bills (the sandbox governs
#     model-run shell commands, not codex's own API calls), so the only symptom
#     is DNS failures buried in the transcript. With network on, "cannot push
#     upstream" is enforced by the spec guardrails below rather than by the
#     sandbox, so keep that line in GUARDRAILS.
#   - repo conventions live in AGENTS.md at the repo root, which codex reads
#     automatically from the worktree; GUARDRAILS carries only the
#     invocation-specific rules
#   - the full transcript is written to <workdir>/.codex-dispatch/<stamp>.log
#     and kept for triage; stdout carries only the worker's final message and
#     the log path, so tool results stay small
set -euo pipefail

QUOTA_EXIT=42
BUSY_EXIT=65

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
--- Repo guardrails (do not violate; AGENTS.md at the repo root is the full contract) ---
- Do NOT run git commit, git push, or any git state-changing command. The
  orchestrator reviews and commits your diff. The sandbox has network access,
  so this is on you to respect: nothing stops a push at the sandbox layer.
- Do NOT run bazel, go test, or npm test on this machine. Targeted pytest on
  the specific hermetic Python test files you edited is allowed and
  encouraged; a local pass is advisory, the orchestrator runs ci on Linux as
  the gate.
- Never use em-dashes in anything you write.
- When done, print a short summary of files changed and any open questions.'

RUNDIR="$WORKDIR/.codex-dispatch"
mkdir -p "$RUNDIR"

# Single-flight: one worker per worktree. A second dispatch into a worktree
# with a live worker interleaves edits and has produced corrupted diffs, so
# refuse loudly instead. A lock whose pid is dead is stale and reclaimed.
LOCK="$RUNDIR/lock"
if ! mkdir "$LOCK" 2>/dev/null; then
	if ! OTHER_PID="$(cat "$LOCK/pid" 2>/dev/null)" || [[ -z "$OTHER_PID" ]]; then
		echo "dispatch lock is being acquired for $WORKDIR" >&2
		exit "$BUSY_EXIT"
	fi
	OTHER_CHILD=""
	if [[ -e "$LOCK/child" ]]; then
		if ! OTHER_CHILD="$(cat "$LOCK/child" 2>/dev/null)"; then
			echo "dispatch lock metadata is unreadable for $WORKDIR" >&2
			exit "$BUSY_EXIT"
		fi
		if [[ -n "$OTHER_CHILD" ]] && kill -0 "$OTHER_CHILD" 2>/dev/null; then
			echo "dispatch child pid $OTHER_CHILD already owns $WORKDIR" >&2
			echo "live log: $(cat "$LOCK/log" 2>/dev/null || echo unknown)" >&2
			exit "$BUSY_EXIT"
		fi
	fi
	if kill -0 "$OTHER_PID" 2>/dev/null; then
		echo "dispatch pid $OTHER_PID already owns $WORKDIR" >&2
		echo "live log: $(cat "$LOCK/log" 2>/dev/null || echo unknown)" >&2
		exit "$BUSY_EXIT"
	fi
	rm -rf "$LOCK"
	if ! mkdir "$LOCK" 2>/dev/null; then
		echo "dispatch lock was claimed by another invocation for $WORKDIR" >&2
		exit "$BUSY_EXIT"
	fi
fi
echo $$ >"$LOCK/pid"

STAMP="$(date +%Y%m%dT%H%M%S).$$"
LOG="$RUNDIR/$STAMP.log"
LAST="$RUNDIR/$STAMP.last-message.txt"
echo "$LOG" >"$LOCK/log"

# If the harness kills this wrapper (timeout, user interrupt), take the codex
# child down too: an orphaned worker keeps editing the worktree underneath
# whatever retry follows.
CHILD=""
cleanup() {
	if [[ -n "$CHILD" ]]; then
		kill -TERM -- -$CHILD 2>/dev/null || true
		wait "$CHILD" 2>/dev/null || true
		CHILD=""
	fi
	rm -rf "$LOCK"
}
trap cleanup EXIT
trap 'cleanup; trap - TERM; exit 143' TERM INT

rm -f "$LAST"
set -m
set +e
codex exec \
	--model "$MODEL" \
	--config model_reasoning_effort="$EFFORT" \
	--config sandbox_workspace_write.network_access=true \
	--sandbox workspace-write \
	--skip-git-repo-check \
	--output-last-message "$LAST" \
	-C "$WORKDIR" \
	"${SPEC}${GUARDRAILS}" >"$LOG" 2>&1 &
CHILD=$!
echo "$CHILD" >"$LOCK/child"
wait "$CHILD"
CODE=$?
CHILD=""
set -e

# Quota / rate-limit detection: codex surfaces plan exhaustion as an error
# line rather than a distinct exit code, so grep the transcript. Gate on a
# non-zero exit so a task spec that merely mentions rate limits or 429s
# does not false-positive.
if [[ "$CODE" -ne 0 ]] &&
	grep -qiE 'usage limit|plan limit|rate limit|too many requests|quota exceeded|http/?[12]?[.0-9]* 429|status(:)? 429' "$LOG"; then
	echo "CODEX_QUOTA_EXHAUSTED model=$MODEL log=$LOG" >&2
	exit "$QUOTA_EXIT"
fi

echo "--- codex exit $CODE (model=$MODEL, transcript: $LOG) ---"
if [[ -s "$LAST" ]]; then
	cat "$LAST"
else
	echo "(no final message captured; last 40 transcript lines follow)"
	tail -40 "$LOG"
fi

exit "$CODE"
