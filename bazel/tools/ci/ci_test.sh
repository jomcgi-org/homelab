#!/usr/bin/env bash
# Hermetic checks for bazel/tools/ci/ci (no git repo, no bb CLI required).
set -euo pipefail

CI_REL="bazel/tools/ci/ci"
CI=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${CI_REL}" \
	"${TEST_SRCDIR:-}/_main/${CI_REL}" \
	"${BASH_SOURCE[0]%/*}/ci"; do
	if [[ -f "$candidate" ]]; then
		CI="$(cd "${candidate%/*}" && pwd)/${candidate##*/}"
		break
	fi
done
if [[ -z "$CI" ]]; then
	echo "ERROR: cannot locate ci script" >&2
	exit 1
fi

BUILD_BUDDY=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/buildbuddy.yaml" \
	"${TEST_SRCDIR:-}/_main/buildbuddy.yaml" \
	"${BASH_SOURCE[0]%/*}/../../..//buildbuddy.yaml"; do
	if [[ -f "$candidate" ]]; then
		BUILD_BUDDY="$(cd "${candidate%/*}" && pwd)/${candidate##*/}"
		break
	fi
done
if [[ -z "$BUILD_BUDDY" ]]; then
	echo "ERROR: cannot locate buildbuddy.yaml" >&2
	exit 1
fi

PASS=0
FAIL=0
pass() {
	echo "PASS [$1]"
	PASS=$((PASS + 1))
}
fail() {
	echo "FAIL [$1]: $2"
	FAIL=$((FAIL + 1))
}

resource_requests="$(awk '
	$0 ~ /^  - name: "pr-checks"$/ { in_pr_checks = 1; next }
	in_pr_checks && $0 ~ /^  - name: / { exit }
	in_pr_checks && $0 ~ /^    resource_requests:$/ { in_resources = 1; next }
	in_resources && $0 ~ /^      [a-z]+:/ { parse_resource = 1 }
	in_resources && $0 ~ /^[[:space:]]*(#|$)/ { next }
	in_resources && !parse_resource { exit }
	in_resources {
		key = $1
		sub(/:$/, "", key)
		value = $2
		sub(/^"/, "", value)
		sub(/"$/, "", value)
		print key "=" value
		parse_resource = 0
	}
' "$BUILD_BUDDY")"

resource_properties=()
while IFS= read -r property; do
	[[ -n "$property" ]] && resource_properties+=("$property")
done < <(grep -oE -- '--runner_exec_properties=[^[:space:]\\]+=[^[:space:]\\]+' "$CI" |
	sed 's/.*--runner_exec_properties=//')

# Record each drift with its specific cause, and do not stop at the first one.
# The whole reason this test exists is that the drift is otherwise silent, so a
# failure that only says "they do not match" leaves the reader doing by hand the
# comparison the test just did.
drift=()

while IFS='=' read -r yaml_key _; do
	case "$yaml_key" in
	'') ;;
	cpu | memory | disk) ;;
	*) drift+=("buildbuddy.yaml pr-checks declares resource_requests key '$yaml_key', which this test has no mapping for: add it here and pass it in cmd_test") ;;
	esac
done <<<"$resource_requests"

for yaml_key in cpu memory disk; do
	case "$yaml_key" in
	cpu) property="EstimatedCPU" ;;
	memory) property="EstimatedMemory" ;;
	disk) property="EstimatedFreeDiskBytes" ;;
	esac
	yaml_value="$(printf '%s\n' "$resource_requests" | awk -F= -v key="$yaml_key" '$1 == key { print $2 }')"
	ci_value=""
	for candidate in "${resource_properties[@]}"; do
		if [[ "${candidate%%=*}" == "$property" ]]; then
			ci_value="${candidate#*=}"
		fi
	done
	if [[ -z "$yaml_value" ]]; then
		drift+=("buildbuddy.yaml pr-checks declares no '$yaml_key' under resource_requests (parse failure, or the key was removed)")
	elif [[ -z "$ci_value" ]]; then
		drift+=("ci cmd_test passes no $property, but buildbuddy.yaml pr-checks declares $yaml_key: $yaml_value")
	elif [[ "$yaml_value" != "$ci_value" ]]; then
		drift+=("ci cmd_test passes $property=$ci_value, but buildbuddy.yaml pr-checks declares $yaml_key: $yaml_value")
	fi
done

for property in "${resource_properties[@]}"; do
	property_name="${property%%=*}"
	case "$property_name" in
	EstimatedCPU | EstimatedMemory | EstimatedFreeDiskBytes | EstimatedComputeUnits) ;;
	Estimated*)
		drift+=("ci cmd_test passes $property_name, which BuildBuddy does not recognise: it will silently fall back to the default rather than fail")
		;;
	esac
done

if [[ ${#drift[@]} -eq 0 ]]; then
	pass "buildbuddy_resource_parity"
else
	fail "buildbuddy_resource_parity" "$(printf '%s | ' "${drift[@]}")"
fi

if grep -q 'local feedback loop' "$CI" &&
	grep -q 'SKIP_REMOTE=1' "$CI" &&
	grep -q 'include-secrets=true' "$CI"; then
	pass "header_docs"
else
	fail "header_docs" "missing usage/docs markers"
fi

if grep -q 'deleted_packages=bazel/tools/python' "$CI" &&
	grep -q 'test_tag_filters=-external,-future' "$CI" &&
	grep -qF '//...' "$CI"; then
	pass "ci_test_argv_locked"
else
	fail "ci_test_argv_locked" "missing locked Test flags"
fi

flag_ln=$(grep -n "unexpected flag" "$CI" | head -1 | cut -d: -f1)
bb_ln=$(grep -n "command -v bb" "$CI" | head -1 | cut -d: -f1)
if [[ -n "$flag_ln" && -n "$bb_ln" && "$flag_ln" -lt "$bb_ln" ]]; then
	pass "flags_before_bb"
else
	fail "flags_before_bb" "flag parse (line $flag_ln) must precede bb check (line $bb_ln)"
fi

if grep -qE '\*\.py\)' "$CI" && grep -qE '\*\.js' "$CI" && grep -qE '\*\.svelte' "$CI"; then
	pass "lint_extensions"
else
	fail "lint_extensions" "missing py/js/svelte cases"
fi

if grep -q 'need_generators' "$CI" && grep -q 'need_gazelle' "$CI"; then
	pass "regen_helpers"
else
	fail "regen_helpers" "missing need_* helpers"
fi

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
FAKE_ROOT="$TMP/ci-fake-root"
STUB_BIN="$TMP/ci-stub-bin"
mkdir -p "$FAKE_ROOT" "$STUB_BIN"

cat >"$STUB_BIN/git" <<'EOF'
#!/usr/bin/env bash
if [[ "$1 ${2:-}" == "rev-parse --show-toplevel" ]]; then
	echo "$CI_FAKE_ROOT"
fi
exit 0
EOF
chmod +x "$STUB_BIN/git"

cat >"$STUB_BIN/bb" <<'EOF'
#!/usr/bin/env bash
cat "$BB_FIXTURE"
exit "${BB_STATUS:-0}"
EOF
chmod +x "$STUB_BIN/bb"

cat >"$TMP/green_run" <<'EOF'
INFO: Analyzed 361 targets
Executed 3 out of 361 tests: 361 tests pass.
EOF
cat >"$TMP/singular_green_run" <<'EOF'
INFO: Analyzed 1 target
Executed 1 out of 1 test: 1 test passes.
EOF
cat >"$TMP/action_failed" <<'EOF'
Command failed: exit status 1

Action failed: failed to set up git repo: exit status 1
Remote run completed at 2026-07-28 05:20:10 UTC
EOF
cat >"$TMP/missing_summary" <<'EOF'
INFO: Analyzed 361 targets
INFO: Build completed successfully, 361 total actions
EOF
cat >"$TMP/bb_failure" <<'EOF'
Executed 3 out of 361 tests: 361 tests pass.
EOF
cat >"$TMP/summary_reports_failures" <<'EOF'
//projects/foo:bar    FAILED in 0.4s
Executed 361 out of 361 tests: 358 tests pass, 3 fail locally.
EOF
{
	printf '\033[31mAction failed: failed to start remote runner\033[0m\n'
	printf '\033[31mCommand failed: exit status 1\033[0m\n'
} >"$TMP/ansi_prefixed_markers_caught"
cat >"$TMP/midline_marker_not_infra" <<'EOF'
INFO: Analyzed 5 targets
expected output: Command failed: exit status 1 (from fixture)
Executed 5 out of 5 tests: 5 tests pass.
EOF
cat >"$TMP/bb_status_3_propagates" <<'EOF'
Executed 5 out of 5 tests: 5 tests pass.
EOF
cat >"$TMP/red_run_with_runner_markers" <<'EOF'
Executed 10 out of 10 tests: 8 tests pass, 2 fail locally.
//projects/foo:bar FAILED in 2s
Command failed: exit status 3
Action failed: exit status 1
EOF
cat >"$TMP/singular_red_run_with_runner_markers" <<'EOF'
//projects/foo:bar    FAILED in 0.4s
Executed 1 out of 1 test: 1 fails locally.
Command failed: exit status 3
Action failed: exit status 1
EOF

run_behavioral_case() {
	local name="$1"
	local fixture="$2"
	local bb_status="$3"
	local want_status="$4"
	local output="$TMP/$name.out"
	local home="$TMP/$name-home"
	local xdg_cache_home="$TMP/$name-xdg-cache"
	local got_status=0
	mkdir -p "$home" "$xdg_cache_home"

	if (cd "$FAKE_ROOT" && HOME="$home" XDG_CACHE_HOME="$xdg_cache_home" \
		PATH="$STUB_BIN:$PATH" CI_FAKE_ROOT="$FAKE_ROOT" \
		BB_FIXTURE="$fixture" BB_STATUS="$bb_status" "$CI" test >"$output" 2>&1); then
		got_status=0
	else
		got_status=$?
	fi
	if [[ "$got_status" -eq "$want_status" ]]; then
		pass "$name"
	else
		fail "$name" "expected exit $want_status, got $got_status: $(tr '\n' ' ' <"$output")"
	fi
}

run_behavioral_case "green_run_passes" "$TMP/green_run" 0 0
run_behavioral_case "singular_green_passes" "$TMP/singular_green_run" 0 0
run_behavioral_case "action_failed_caught" "$TMP/action_failed" 0 1
run_behavioral_case "missing_summary_caught" "$TMP/missing_summary" 0 1
run_behavioral_case "bb_failure_propagates" "$TMP/bb_failure" 1 1
run_behavioral_case "summary_reports_failures_caught" "$TMP/summary_reports_failures" 0 1
run_behavioral_case "ansi_prefixed_markers_caught" "$TMP/ansi_prefixed_markers_caught" 0 1
run_behavioral_case "midline_marker_not_infra" "$TMP/midline_marker_not_infra" 0 0
run_behavioral_case "bb_status_3_propagates" "$TMP/bb_status_3_propagates" 3 3

red_run_output="$TMP/red_run_diagnosed_as_red.out"
red_run_status=0
if (cd "$FAKE_ROOT" && HOME="$TMP/red_run_diagnosed_as_red-home" \
	XDG_CACHE_HOME="$TMP/red_run_diagnosed_as_red-xdg-cache" \
	PATH="$STUB_BIN:$PATH" CI_FAKE_ROOT="$FAKE_ROOT" \
	BB_FIXTURE="$TMP/red_run_with_runner_markers" BB_STATUS=0 "$CI" test \
	>"$red_run_output" 2>&1); then
	red_run_status=0
else
	red_run_status=$?
fi
if [[ "$red_run_status" -ne 0 ]] &&
	grep -qF "the run reported test failures, so this is a red run" "$red_run_output" &&
	! grep -qF "remote runner failed" "$red_run_output"; then
	pass "red_run_diagnosed_as_red"
else
	fail "red_run_diagnosed_as_red" "expected red-run diagnosis, got exit $red_run_status: $(tr '\n' ' ' <"$red_run_output")"
fi

singular_red_stderr="$TMP/singular_red_diagnosed.err"
singular_red_stdout="$TMP/singular_red_diagnosed.out"
singular_red_status=0
if (cd "$FAKE_ROOT" && HOME="$TMP/singular_red_diagnosed-home" \
	XDG_CACHE_HOME="$TMP/singular_red_diagnosed-xdg-cache" \
	PATH="$STUB_BIN:$PATH" CI_FAKE_ROOT="$FAKE_ROOT" \
	BB_FIXTURE="$TMP/singular_red_run_with_runner_markers" BB_STATUS=0 "$CI" test \
	>"$singular_red_stdout" 2>"$singular_red_stderr"); then
	singular_red_status=0
else
	singular_red_status=$?
fi
if [[ "$singular_red_status" -ne 0 ]] &&
	grep -qF "the run reported test failures, so this is a red run" "$singular_red_stderr" &&
	! grep -qF "remote runner failed" "$singular_red_stderr"; then
	pass "singular_red_diagnosed"
else
	fail "singular_red_diagnosed" "expected red-run diagnosis, got exit $singular_red_status: $(tr '\n' ' ' <"$singular_red_stderr")"
fi

echo "--- $PASS passed, $FAIL failed ---"
[[ $FAIL -eq 0 ]]
