#!/usr/bin/env bash
# Unit tests for validate-hooks-executable.sh.
#
# The script:
#   - Takes an optional repo-root argument (defaults to `git rev-parse
#     --show-toplevel`)
#   - Parses .claude/settings.json, walking every hooks[<event>][].hooks[]
#     entry across all events and matchers
#   - Substitutes $CLAUDE_PROJECT_DIR with the repo root in each command
#   - Exits 0 when every resulting path exists and is executable
#   - Exits 1, listing every failing path and its problem, otherwise

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate script under test from Bazel runfiles
# ---------------------------------------------------------------------------
SCRIPT_REL="bazel/tools/hooks/validate-hooks-executable.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/validate-hooks-executable.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$candidate"
		break
	fi
done
if [[ -z "$SCRIPT" ]]; then
	echo "ERROR: cannot locate validate-hooks-executable.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a jq stub covering the one expression validate-hooks-executable.sh
# issues:
#   .hooks // {} | to_entries[] | .value[]? | .hooks[]? | .command // empty
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""jq stub covering the single expression used by validate-hooks-executable.sh."""
import json, sys

args = sys.argv[1:]
if args and args[0] == "-r":
    args = args[1:]
expr = args[0] if args else "."

with open(args[-1]) as f:
    data = json.load(f)

EXPECTED = ".hooks // {} | to_entries[] | .value[]? | .hooks[]? | .command // empty"
if expr != EXPECTED:
    print(f"jq stub: unhandled expression: {expr}", file=sys.stderr)
    sys.exit(1)

hooks = data.get("hooks") or {}
for matchers in hooks.values():
    if not isinstance(matchers, list):
        continue
    for matcher_entry in matchers:
        for h in (matcher_entry.get("hooks") or []):
            cmd = h.get("command")
            if cmd:
                print(cmd)
JQ_STUB
chmod +x "${TEST_TMPDIR}/bin/jq"
export PATH="${TEST_TMPDIR}/bin:${PATH}"

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

run_test() {
	local name="$1"
	local repo_root="$2"
	local want_exit="$3"
	local want_stderr_re="$4" # regex that must match stderr (empty = no output expected)

	local stderr_out
	local got_exit=0
	stderr_out=$(bash "$SCRIPT" "$repo_root" 2>&1 >/dev/null) || got_exit=$?

	local ok=true

	if [[ "$got_exit" -ne "$want_exit" ]]; then
		echo "FAIL [$name]: exit $got_exit, want $want_exit"
		ok=false
	fi

	if [[ -n "$want_stderr_re" ]]; then
		if ! echo "$stderr_out" | grep -qE "$want_stderr_re"; then
			echo "FAIL [$name]: stderr $(printf '%q' "$stderr_out") did not match /$want_stderr_re/"
			ok=false
		fi
	else
		if [[ -n "$stderr_out" ]]; then
			echo "FAIL [$name]: unexpected stderr: $(printf '%q' "$stderr_out")"
			ok=false
		fi
	fi

	if $ok; then
		echo "PASS [$name]"
		PASS=$((PASS + 1))
	else
		FAIL=$((FAIL + 1))
	fi
}

write_settings() {
	local repo_root="$1"
	local body="$2"
	mkdir -p "${repo_root}/.claude"
	printf '%s' "$body" >"${repo_root}/.claude/settings.json"
}

make_dummy() {
	local path="$1"
	local executable="$2"
	mkdir -p "$(dirname "$path")"
	printf '#!/bin/bash\nexit 0\n' >"$path"
	if [[ "$executable" == "yes" ]]; then
		chmod +x "$path"
	else
		chmod -x "$path"
	fi
}

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# 1. All hook commands exist and are executable → passes
REPO1="${TEST_TMPDIR}/repo1"
make_dummy "${REPO1}/bazel/tools/hooks/one.sh" yes
make_dummy "${REPO1}/bazel/tools/hooks/two.sh" yes
write_settings "$REPO1" '{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {"type": "command", "command": "$CLAUDE_PROJECT_DIR/bazel/tools/hooks/one.sh", "timeout": 10},
          {"type": "command", "command": "$CLAUDE_PROJECT_DIR/bazel/tools/hooks/two.sh", "timeout": 10}
        ]
      }
    ]
  }
}'
run_test "all_executable_passes" "$REPO1" 0 ""

# 2. A hook command referencing a non-executable file fails and names it
REPO2="${TEST_TMPDIR}/repo2"
make_dummy "${REPO2}/bazel/tools/hooks/notexec.sh" no
write_settings "$REPO2" '{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {"type": "command", "command": "$CLAUDE_PROJECT_DIR/bazel/tools/hooks/notexec.sh", "timeout": 10}
        ]
      }
    ]
  }
}'
run_test "non_executable_fails_and_names_it" "$REPO2" 1 "bazel/tools/hooks/notexec\.sh: not executable"

# 3. A hook command referencing a missing file fails and names it
REPO3="${TEST_TMPDIR}/repo3"
mkdir -p "${REPO3}/bazel/tools/hooks"
write_settings "$REPO3" '{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "$CLAUDE_PROJECT_DIR/bazel/tools/hooks/missing.sh", "timeout": 10}
        ]
      }
    ]
  }
}'
run_test "missing_file_fails" "$REPO3" 1 "bazel/tools/hooks/missing\.sh: does not exist"

# 4. Failures across multiple events/matchers are all reported together
REPO4="${TEST_TMPDIR}/repo4"
make_dummy "${REPO4}/bazel/tools/hooks/ok.sh" yes
make_dummy "${REPO4}/bazel/tools/hooks/bad.sh" no
write_settings "$REPO4" '{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [
        {"type": "command", "command": "$CLAUDE_PROJECT_DIR/bazel/tools/hooks/ok.sh", "timeout": 10}
      ]}
    ],
    "PostToolUse": [
      {"matcher": "Write|Edit", "hooks": [
        {"type": "command", "command": "$CLAUDE_PROJECT_DIR/bazel/tools/hooks/bad.sh", "timeout": 10},
        {"type": "command", "command": "$CLAUDE_PROJECT_DIR/bazel/tools/hooks/gone.sh", "timeout": 10}
      ]}
    ]
  }
}'
run_test "multiple_failures_all_listed_bad" "$REPO4" 1 "bad\.sh: not executable"
run_test "multiple_failures_all_listed_gone" "$REPO4" 1 "gone\.sh: does not exist"

# 5. No "hooks" key at all → nothing to validate, passes
REPO5="${TEST_TMPDIR}/repo5"
write_settings "$REPO5" '{"cleanupPeriodDays": 60}'
run_test "no_hooks_key_passes" "$REPO5" 0 ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ "$FAIL" -gt 0 ]]; then
	exit 1
fi
exit 0
