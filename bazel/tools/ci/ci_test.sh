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

LOCAL_AFFECTED_TEST=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/bazel/tools/ci/local-affected-test.sh" \
	"${TEST_SRCDIR:-}/_main/bazel/tools/ci/local-affected-test.sh" \
	"${BASH_SOURCE[0]%/*}/local-affected-test.sh"; do
	if [[ -f "$candidate" ]]; then
		LOCAL_AFFECTED_TEST="$(cd "${candidate%/*}" && pwd)/${candidate##*/}"
		break
	fi
done
if [[ -z "$LOCAL_AFFECTED_TEST" ]]; then
	echo "ERROR: cannot locate local-affected-test.sh" >&2
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

check_resource_parity() {
	local -a resource_properties=()
	while IFS= read -r property; do
		[[ -n "$property" ]] && resource_properties+=("$property")
	done < <(sed -n 's/^--runner_exec_properties=//p' "$1")

	# Record each drift with its specific cause, and do not stop at the first one.
	# The whole reason this test exists is that the drift is otherwise silent, so a
	# failure that only says "they do not match" leaves the reader doing by hand the
	# comparison the test just did.
	local -a drift=()

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

	if [[ ${#drift[@]} -ne 0 ]]; then
		printf "%s\n" "${drift[@]}" >&2
		return 1
	fi
}

# Keep deployment folded into the required check. Action names are part of
# BuildBuddy's snapshot key, so accidentally restoring a separate deploy action
# would also restore duplicate main tests and disjoint snapshot state.
action_names="$(awk '
	$0 ~ /^  - name: / {
		name = $0
		sub(/^  - name: "/, "", name)
		sub(/"$/, "", name)
		print name
	}
' "$BUILD_BUDDY")"
if [[ "$action_names" == "pr-checks" ]]; then
	pass "buildbuddy_single_action"
else
	fail "buildbuddy_single_action" "expected only pr-checks, found: ${action_names//$'\n'/, }"
fi

pr_checks_shape="$(awk '
	$0 == "  - name: \"pr-checks\"" { in_action = 1 }
	in_action && $0 ~ /^  - name: / && $0 != "  - name: \"pr-checks\"" { exit }
	in_action { print }
' "$BUILD_BUDDY")"
if grep -q '^    timeout: "2h"$' <<<"$pr_checks_shape" &&
	grep -q '^    git_fetch_depth: 0$' <<<"$pr_checks_shape"; then
	pass "buildbuddy_pr_checks_runner_shape"
else
	fail "buildbuddy_pr_checks_runner_shape" "pr-checks must declare timeout 2h and git_fetch_depth 0"
fi

if grep -Fq '[ -n "${GIT_BRANCH:-}" ]' <<<"$pr_checks_shape" &&
	grep -Fq '[ "${GIT_BRANCH:-}" = "${GIT_REPO_DEFAULT_BRANCH:-}" ]' <<<"$pr_checks_shape" &&
	grep -Fq '[ "${GIT_PR_NUMBER:-0}" = "0" ]' <<<"$pr_checks_shape"; then
	pass "buildbuddy_main_gate"
else
	fail "buildbuddy_main_gate" "main gate must require default-branch identity and a zero PR number"
fi

main_publish_branch="$(awk '
	$0 == "          if [ \"$ON_MAIN\" = true ] && [ \"$SKIP_MAIN_PUBLISH\" = false ]; then" { in_branch = 1 }
	in_branch { print }
	in_branch && $0 == "          fi" { exit }
' "$BUILD_BUDDY")"
if grep -Fq './bazel/images/push/push-changed.sh' <<<"$main_publish_branch" &&
	grep -Fq './bazel/helm/write-back-versions.sh .chart-version-records --record-publication' <<<"$main_publish_branch"; then
	pass "buildbuddy_main_publish_branch"
else
	fail "buildbuddy_main_publish_branch" "main-only branch must publish changed images and write chart versions back"
fi

if grep -Fq 'git log -1 --format=%s' <<<"$pr_checks_shape" &&
	grep -Fq '"chore(charts): publish"*)' <<<"$pr_checks_shape" &&
	! grep -Fq '"style: auto-format"*' <<<"$pr_checks_shape"; then
	pass "buildbuddy_generated_commit_skip"
else
	fail "buildbuddy_generated_commit_skip" "only chart-version write-back heads may skip publish and format work"
fi

full_test_ln=$(grep -nF 'bazel test //... --config=ci' <<<"$pr_checks_shape" | head -1 | cut -d: -f1 || true)
publish_ln=$(grep -nF './bazel/images/push/push-changed.sh' <<<"$pr_checks_shape" | head -1 | cut -d: -f1 || true)
if [[ -n "$full_test_ln" && -n "$publish_ln" && "$full_test_ln" -lt "$publish_ln" ]]; then
	pass "buildbuddy_tests_before_publish"
else
	fail "buildbuddy_tests_before_publish" "full test gate (line $full_test_ln) must precede publish (line $publish_ln)"
fi

main_drift_branch="$(awk '
	$0 == "            elif [ \"$ON_MAIN\" = true ]; then" { in_branch = 1 }
	in_branch && $0 == "            else" { exit }
	in_branch { print }
' <<<"$pr_checks_shape")"
if grep -Fq 'exit 1' <<<"$main_drift_branch" &&
	! grep -Fq 'git push' <<<"$main_drift_branch"; then
	pass "buildbuddy_main_drift_fails_read_only"
else
	fail "buildbuddy_main_drift_fails_read_only" "main drift path must exit 1 without pushing"
fi

if grep -q 'local affected-target feedback' "$CI" &&
	grep -q 'SKIP_REMOTE=1' "$CI" &&
	grep -q 'include-secrets=true' "$CI" &&
	grep -q 'ls-files --others --exclude-standard' "$CI"; then
	pass "header_docs"
else
	fail "header_docs" "missing usage/docs markers"
fi

ratchet_ln=$(grep -n 'ratchets.sh "$base_ref" HEAD' "$BUILD_BUDDY" | head -1 | cut -d: -f1)
reachability_ln=$(grep -n 'test_source_reachability.py' "$BUILD_BUDDY" | head -1 | cut -d: -f1)
affected_ln=$(grep -n 'local-affected-test.sh "$base_ref"' "$BUILD_BUDDY" | head -1 | cut -d: -f1)
if [[ -n "$ratchet_ln" && -n "$affected_ln" && "$ratchet_ln" -lt "$affected_ln" ]] &&
	grep -q 'git fetch origin "${GIT_REPO_DEFAULT_BRANCH:-main}"' "$BUILD_BUDDY" &&
	grep -q 'git merge-base "$base_ref" HEAD' "$BUILD_BUDDY"; then
	pass "central_build_ratchet_in_pr_checks"
else
	fail "central_build_ratchet_in_pr_checks" "ratchet must fetch and resolve the merge base before affected tests"
fi

if [[ -n "$reachability_ln" && -n "$affected_ln" && "$reachability_ln" -lt "$affected_ln" ]]; then
	pass "test_source_reachability_in_pr_checks"
else
	fail "test_source_reachability_in_pr_checks" "test source guard must run before affected-target selection"
fi

if grep -q 'deleted_packages=bazel/tools/python' "$LOCAL_AFFECTED_TEST" &&
	grep -q -- '--config=ci' "$LOCAL_AFFECTED_TEST" &&
	grep -q -- '--script=' "$CI"; then
	pass "ci_affected_argv_locked"
else
	fail "ci_affected_argv_locked" "missing affected script mode or locked Test flags"
fi

for entrypoint in "$BUILD_BUDDY" "$CI" "$LOCAL_AFFECTED_TEST"; do
	if grep -qE -- '--test_tag_filters=.*(external|future)' "$entrypoint"; then
		fail "retired_bdd_exclusions_absent" "obsolete test tag filter in $entrypoint"
	else
		pass "retired_bdd_exclusions_absent:${entrypoint##*/}"
	fi
done

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

if grep -q 'need_generators' "$CI" && grep -q 'need_gazelle' "$CI" &&
	grep -q 'need_helm_deps' "$CI"; then
	pass "regen_helpers"
else
	fail "regen_helpers" "missing need_* helpers"
fi

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
FAKE_ROOT="$TMP/ci-fake-root"
STUB_BIN="$TMP/ci-stub-bin"
mkdir -p "$FAKE_ROOT" "$STUB_BIN"
mkdir -p "$FAKE_ROOT/bazel/tools/ci"
cp "$LOCAL_AFFECTED_TEST" "$FAKE_ROOT/bazel/tools/ci/local-affected-test.sh"
cp "${CI%/*}/ratchets.sh" "${CI%/*}/runner-resources.env" "$FAKE_ROOT/bazel/tools/ci/"

# Execute the workflow's pre-query image-index preparation against a fake
# generator. The read-only heads must never regenerate indexes, and a normal
# PR must reach it before the first Bazel graph query.
index_prepare="$(awk '
    $0 == "          # Regenerate image indexes before any Bazel query analyzes them." { in_prepare = 1 }
    in_prepare && $0 ~ /# ---- 1b\. tracked Python test reachability/ { exit }
    in_prepare { sub(/^          /, ""); print }
' "$BUILD_BUDDY")"
index_prepare_ln=$(grep -nF './bazel/images/generate-push-all.sh' "$BUILD_BUDDY" | head -1 | cut -d: -f1 || true)
if [[ -n "$index_prepare" && -n "$index_prepare_ln" && "$index_prepare_ln" -lt "$reachability_ln" ]]; then
	pass "image_indexes_before_graph_queries"
else
	fail "image_indexes_before_graph_queries" "preparation must precede tracked test reachability"
fi
index_root="$TMP/index-preparation"
mkdir -p "$index_root/bazel/images"
printf '%s\n' '#!/usr/bin/env bash' 'touch generated' >"$index_root/bazel/images/generate-push-all.sh"
chmod +x "$index_root/bazel/images/generate-push-all.sh"
for scenario in pr main queue bot push unnamed; do
	(
		cd "$index_root"
		rm -f generated
		ON_MAIN=false
		GIT_PR_NUMBER=7188
		GIT_BRANCH=codex/retire-demo
		TEST_INDEX_AUTHOR=author
		case "$scenario" in
		main)
			ON_MAIN=true
			GIT_PR_NUMBER=0
			GIT_BRANCH=main
			;;
		queue) GIT_BRANCH=gh-readonly-queue/main/pr-7188 ;;
		bot) TEST_INDEX_AUTHOR=ci-format-bot ;;
		push) GIT_PR_NUMBER=0 ;;
		unnamed) GIT_BRANCH= ;;
		esac
		git() { printf '%s\n' "$TEST_INDEX_AUTHOR"; }
		# Graph generation is exercised separately with a real fixture below.
		prepare_go_build_graph() { :; }
		eval "$index_prepare"
		if [[ "$scenario" == pr ]]; then
			[[ -f generated ]]
		else
			[[ ! -f generated ]]
		fi
	) && pass "image_index_prepare_$scenario" || fail "image_index_prepare_$scenario" "unexpected generation decision"
done

cat >"$STUB_BIN/git" <<'EOF'
#!/usr/bin/env bash
if [[ "$1 ${2:-}" == "rev-parse --show-toplevel" ]]; then
	echo "$CI_FAKE_ROOT"
elif [[ "$1" == "merge-base" ]]; then
	[[ -z "${MERGE_BASE_LOG:-}" ]] || printf '%s\n' "$*" >>"$MERGE_BASE_LOG"
	[[ "${FAIL_MERGE_BASE:-0}" == 0 ]] || exit 1
	echo resolved-merge-base
elif [[ "$1" == "diff" ]]; then
	printf '%s\n' "${CI_CHANGED_FILE:-}"
	for arg in "$@"; do
		if [[ "$arg" == --diff-filter=*D* ]]; then
			printf '%s\n' "${CI_DELETED_FILE:-}"
		fi
	done
fi
exit 0
EOF
chmod +x "$STUB_BIN/git"

cat >"$STUB_BIN/bb" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$BB_LOG"
cat "$BB_FIXTURE"
exit "${BB_STATUS:-0}"
EOF
chmod +x "$STUB_BIN/bb"

cat >"$STUB_BIN/python3" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$GUARD_LOG"
if [[ "${1##*/}" == "${FAIL_GUARD:-}" ]]; then
	exit 7
fi
EOF
chmod +x "$STUB_BIN/python3"

# Run the real regen entry point with a minimal PATH. A developer's installed
# helm or tools cache must not turn the missing-tool case into a false pass.
REGEN_BIN="$TMP/regen-bin"
HELM_BIN="$TMP/helm-bin"
mkdir -p "$REGEN_BIN" "$HELM_BIN" "$FAKE_ROOT/bazel/tools/format"
for tool in bash sed sort; do
	ln -s "$(command -v "$tool")" "$REGEN_BIN/$tool"
done
ln -s "$STUB_BIN/git" "$REGEN_BIN/git"
cat >"$HELM_BIN/helm" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$FAKE_ROOT/bazel/tools/format/sync-helm-deps.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' sync >>"$SYNC_LOG"
exit "${SYNC_STATUS:-0}"
EOF
chmod +x "$HELM_BIN/helm" "$FAKE_ROOT/bazel/tools/format/sync-helm-deps.sh"

run_regen_case() {
	local name="$1" changed="$2" helm="$3" sync_status="$4" expected_status="$5" expected_runs="$6"
	local deleted="${7:-}"
	local regen_path="$REGEN_BIN" actual_status=0 actual_runs=0
	[[ "$helm" != available ]] || regen_path="$HELM_BIN:$regen_path"
	if (cd "$FAKE_ROOT" && XDG_CACHE_HOME="$TMP/regen-cache" \
		PATH="$regen_path" CI_FAKE_ROOT="$FAKE_ROOT" CI_CHANGED_FILE="$changed" CI_DELETED_FILE="$deleted" \
		SYNC_LOG="$TMP/$name.sync" SYNC_STATUS="$sync_status" \
		"$CI" regen >"$TMP/$name.out" 2>&1); then
		actual_status=0
	else
		actual_status=$?
	fi
	if [[ -f "$TMP/$name.sync" ]]; then
		actual_runs=$(wc -l <"$TMP/$name.sync")
	fi
	if [[ "$actual_status" -eq "$expected_status" && "$actual_runs" -eq "$expected_runs" ]]; then
		pass "$name"
	else
		fail "$name" "exit=$actual_status syncs=$actual_runs: $(tr '\n' ' ' <"$TMP/$name.out")"
	fi
}

run_regen_case helm_chart_yaml projects/example/chart/Chart.yaml available 0 0 1
run_regen_case helm_chart_lock projects/example/chart/Chart.lock available 0 0 1
run_regen_case helm_tarball projects/example/chart/charts/library-1.0.0.tgz available 0 0 1
run_regen_case helm_root_yaml Chart.yaml available 0 0 1
run_regen_case helm_root_lock Chart.lock available 0 0 1
run_regen_case helm_root_tarball charts/library-1.0.0.tgz available 0 0 1
run_regen_case helm_shallow_lock example/Chart.lock available 0 0 1
run_regen_case helm_deleted_lock "" available 0 0 1 projects/example/chart/Chart.lock
run_regen_case helm_deleted_tarball "" available 0 0 1 projects/example/chart/charts/library-1.0.0.tgz
run_regen_case helm_deleted_unrelated "" missing 0 0 0 tools/example.txt
run_regen_case helm_unrelated tools/example.txt missing 0 0 0
run_regen_case helm_missing projects/example/chart/Chart.lock missing 0 1 0
run_regen_case helm_sync_failure projects/example/chart/Chart.lock available 7 7 1
if grep -qF './bootstrap.sh' "$TMP/helm_missing.out" &&
	grep -qF 'helm' "$TMP/helm_missing.out"; then
	pass "helm_missing_bootstrap_hint"
else
	fail "helm_missing_bootstrap_hint" "missing helm/bootstrap diagnostic"
fi

cat >"$TMP/green_run" <<'EOF'
INFO: Analyzed 361 targets
Executed 3 out of 361 tests: 361 tests pass.
EOF
cat >"$TMP/singular_green_run" <<'EOF'
INFO: Analyzed 1 target
Executed 1 out of 1 test: 1 test passes.
EOF
cat >"$TMP/no_affected_targets" <<'EOF'
ci-local-affected: no Bazel targets affected; skipping bazel test
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
cat >"$TMP/duplicate_range_apk_download" <<'EOF'
WARNING: Download from https://packages.wolfi.dev/os/x86_64/erlang-27-27.3.4.2-r0.apk failed:
  UnrecoverableHttpException Multiple entries with same key: Range=[bytes=19853679-] and Range=[bytes=3585-55274638]
ERROR: no package
ERROR: Analysis of target '//projects/embervm/image:image_base_amd64' failed; build aborted
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

cat >"$TMP/verification_genrule_failed" <<'EOF'
ci-local-affected: running bazel test on 3 affected target(s)
ERROR: /repo/bazel/erlang/BUILD:91:8: Executing genrule //bazel/erlang:mix_test_smoke failed: (Exit 1)
     1) test the sweeper terminalizes (Embervm.StatefulSweeperTest)
1637 tests, 1 failure
FAILED: Build did NOT complete successfully
EOF
cat >"$TMP/executed_zero_with_verification" <<'EOF'
ci-local-affected: running bazel test on 138 affected target(s)
affected-targets: 138 affected targets, 1 of them verification genrules
Executed 0 out of 137 tests: 137 tests pass.
EOF
cat >"$TMP/executed_zero_without_verification" <<'EOF'
ci-local-affected: running bazel test on 137 affected target(s)
affected-targets: 137 affected targets, 0 of them verification genrules
Executed 0 out of 137 tests: 137 tests pass.
EOF

# Exercise bare ci, including lint/regen, with no changed files in the fake repo.
# Only the Python boundary is stubbed: guard logic has its own test suites.
run_guard_case() {
	local name="$1" base_ref="$2" skip_remote="$3" failed_guard="$4"
	local fail_base="$5" want_status="$6" expected_guards="$7"
	local output="$TMP/$name.out" got_status=0
	local guard_log="$TMP/$name.guards" base_log="$TMP/$name.base"
	local bb_log="$TMP/$name.bb.log"
	local -a command_args=()
	[[ -z "${8:-}" ]] || command_args+=("$8")
	: >"$guard_log"
	: >"$base_log"
	if (cd "$FAKE_ROOT" && XDG_CACHE_HOME="$TMP/$name-cache" \
		PATH="$STUB_BIN:$PATH" CI_FAKE_ROOT="$FAKE_ROOT" \
		CI_BASE_REF="$base_ref" SKIP_REMOTE="$skip_remote" \
		GUARD_LOG="$guard_log" MERGE_BASE_LOG="$base_log" \
		FAIL_GUARD="$failed_guard" FAIL_MERGE_BASE="$fail_base" \
		BB_FIXTURE="$TMP/green_run" BB_STATUS=0 BB_LOG="$bb_log" \
		"$CI" "${command_args[@]}" >"$output" 2>&1); then
		got_status=0
	else
		got_status=$?
	fi
	if [[ "$got_status" -eq "$want_status" ]]; then
		pass "$name status"
	else
		fail "$name status" "expected $want_status, got $got_status: $(tr '\n' ' ' <"$output")"
	fi
	if [[ "$(<"$guard_log")" == "$expected_guards" ]]; then
		pass "$name guard arguments and order"
	else
		fail "$name guard arguments and order" "expected [$expected_guards], got [$(<"$guard_log")]"
	fi
	if [[ "$(grep -cxF "merge-base ${base_ref:-origin/main} HEAD" "$base_log" || true)" == 1 ]]; then
		pass "$name merge base"
	else
		fail "$name merge base" "expected one ratchet resolution for ${base_ref:-origin/main}: $(<"$base_log")"
	fi
	if [[ "$want_status" == 0 && "$skip_remote" == 0 ]]; then
		if [[ -s "$bb_log" ]]; then
			pass "$name remote reached"
		else
			fail "$name remote reached" "bb was not invoked"
		fi
	elif [[ ! -e "$bb_log" ]]; then
		pass "$name remote prevented"
	else
		fail "$name remote prevented" "bb must not run after a guard error or with SKIP_REMOTE=1"
	fi
}

guard_calls=""
for guard in verification_genrule_guard.py central_build_ratchet.py chart_version_guard.py source_ratchet.py; do
	if [[ "$guard" == verification_genrule_guard.py ]]; then
		guard_calls="./bazel/tools/ci/$guard ."
	else
		guard_calls+=$'\n'"./bazel/tools/ci/$guard resolved-merge-base HEAD"
	fi
	for skip in 0 1; do
		run_guard_case "guard_failure_${guard}_${skip}" "" "$skip" "$guard" 0 7 "$guard_calls"
	done
done
run_guard_case guards_default_base "" 0 "" 0 0 "$guard_calls"
run_guard_case explicit_ratchet "" 1 "" 0 0 "$guard_calls" ratchet
run_guard_case guards_custom_base origin/release 0 "" 0 0 "$guard_calls"
run_guard_case guards_skip_remote "" 1 "" 0 0 "$guard_calls"
run_guard_case guards_missing_base origin/missing 0 "" 1 1 ""
if grep -qF 'ratchet: cannot find the merge base of origin/missing and HEAD' "$TMP/guards_missing_base.out"; then
	pass "missing base diagnostic"
else
	fail "missing base diagnostic" "missing fail-closed ratchet diagnostic"
fi

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
		BB_FIXTURE="$fixture" BB_STATUS="$bb_status" BB_LOG="$TMP/$name.bb.log" \
		"$CI" test >"$output" 2>&1); then
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
# Compare sourced constants, emitted bb arguments and static YAML independently.
source "${CI%/*}/runner-resources.env"
if [[ "$resource_requests" == "cpu=$CI_RUNNER_CPU"$'\n'"memory=$CI_RUNNER_MEMORY"$'\n'"disk=$CI_RUNNER_DISK" ]] &&
	check_resource_parity "$TMP/green_run_passes.bb.log"; then
	pass "buildbuddy_resource_parity_runtime"
else
	fail "buildbuddy_resource_parity_runtime" "sourced values, YAML and emitted bb arguments must agree"
fi
sed 's/EstimatedCPU=8/EstimatedCPU=6/' "$TMP/green_run_passes.bb.log" >"$TMP/wrong-resources"
if check_resource_parity "$TMP/wrong-resources" >"$TMP/wrong-resources.out" 2>&1; then
	fail "resource_mismatch_control" "mismatch passed"
elif grep -qF 'EstimatedCPU=6' "$TMP/wrong-resources.out"; then
	pass "resource_mismatch_control"
else
	fail "resource_mismatch_control" "missing specific diagnostic"
fi
sed 's/EstimatedCPU=/EstimatedCPUs=/' "$TMP/green_run_passes.bb.log" >"$TMP/typo-resources"
if check_resource_parity "$TMP/typo-resources" >"$TMP/typo-resources.out" 2>&1; then
	fail "resource_typo_control" "unknown property passed"
elif grep -qF 'does not recognise' "$TMP/typo-resources.out" &&
	grep -qF 'passes no EstimatedCPU' "$TMP/typo-resources.out"; then
	pass "resource_typo_control"
else
	fail "resource_typo_control" "missing unknown-property or missing-property diagnostic"
fi
saved_requests="$resource_requests"
resource_requests+=$'\n'"accelerators=1"
if check_resource_parity "$TMP/green_run_passes.bb.log" >"$TMP/unknown-resource.out" 2>&1; then
	fail "unknown_yaml_resource_control" "unmapped YAML key passed"
elif grep -qF "key 'accelerators'" "$TMP/unknown-resource.out"; then
	pass "unknown_yaml_resource_control"
else
	fail "unknown_yaml_resource_control" "missing unmapped-key diagnostic"
fi
resource_requests="$saved_requests"
run_behavioral_case "singular_green_passes" "$TMP/singular_green_run" 0 0
run_behavioral_case "no_affected_targets_passes" "$TMP/no_affected_targets" 0 0
run_behavioral_case "action_failed_caught" "$TMP/action_failed" 0 1
run_behavioral_case "missing_summary_caught" "$TMP/missing_summary" 0 1
run_behavioral_case "bb_failure_propagates" "$TMP/bb_failure" 1 1
run_behavioral_case "summary_reports_failures_caught" "$TMP/summary_reports_failures" 0 1
run_behavioral_case "ansi_prefixed_markers_caught" "$TMP/ansi_prefixed_markers_caught" 0 1
run_behavioral_case "midline_marker_not_infra" "$TMP/midline_marker_not_infra" 0 0
run_behavioral_case "bb_status_3_propagates" "$TMP/bb_status_3_propagates" 3 3
run_behavioral_case "verification_genrule_failure_caught" "$TMP/verification_genrule_failed" 0 1
run_behavioral_case "executed_zero_with_verification_passes" "$TMP/executed_zero_with_verification" 0 0
run_behavioral_case "executed_zero_without_verification_passes" "$TMP/executed_zero_without_verification" 0 0

# A red verification genrule must read as a red suite, not as the #4118 infra arm.
if grep -qF "a build action failed" "$TMP/verification_genrule_failure_caught.out" &&
	! grep -qF "no bazel test summary was found" "$TMP/verification_genrule_failure_caught.out"; then
	pass "verification_genrule_failure_reads_as_red"
else
	fail "verification_genrule_failure_reads_as_red" \
		"$(tr '\n' ' ' <"$TMP/verification_genrule_failure_caught.out")"
fi

# The Executed-0 note is about cache hits. A run carrying a verification genrule
# executed a suite that never enters the test count, so the note would be a lie.
if ! grep -qF "issue #5538" "$TMP/executed_zero_with_verification_passes.out" &&
	grep -qF "issue #5538" "$TMP/executed_zero_without_verification_passes.out"; then
	pass "executed_zero_note_skips_verification_runs"
else
	fail "executed_zero_note_skips_verification_runs" \
		"with=$(tr '\n' ' ' <"$TMP/executed_zero_with_verification_passes.out") without=$(tr '\n' ' ' <"$TMP/executed_zero_without_verification_passes.out")"
fi

if grep -qxF -- "--env=CI_BASE_REF=origin/main" "$TMP/green_run_passes.bb.log" &&
	grep -qF -- "--script=#!/usr/bin/env bash" "$TMP/green_run_passes.bb.log" &&
	grep -qF -- "--config=ci" "$TMP/green_run_passes.bb.log" &&
	! grep -qxF "run" "$TMP/green_run_passes.bb.log"; then
	pass "default_uses_affected_runner"
else
	fail "default_uses_affected_runner" "args=$(tr '\n' ' ' <"$TMP/green_run_passes.bb.log")"
fi
if [[ "$(sed -n '/^--script=/,$p' "$TMP/green_run_passes.bb.log")" == "--script=$(cat "$LOCAL_AFFECTED_TEST")" ]] &&
	grep -qxF -- '--runner_exec_properties=include-secrets=true' "$TMP/green_run_passes.bb.log"; then
	pass "exact_shared_script_and_secret_scope"
else
	fail "exact_shared_script_and_secret_scope" "embedded script or secret access changed"
fi

explicit_output="$TMP/explicit_target.out"
if (cd "$FAKE_ROOT" && HOME="$TMP/explicit-home" XDG_CACHE_HOME="$TMP/explicit-cache" \
	PATH="$STUB_BIN:$PATH" CI_FAKE_ROOT="$FAKE_ROOT" \
	BB_FIXTURE="$TMP/singular_green_run" BB_STATUS=0 BB_LOG="$TMP/explicit.bb.log" \
	"$CI" test -- //projects/example:test >"$explicit_output" 2>&1) &&
	grep -qxF "test" "$TMP/explicit.bb.log" &&
	grep -qxF "//projects/example:test" "$TMP/explicit.bb.log" &&
	! grep -qF -- "--script=" "$TMP/explicit.bb.log"; then
	pass "explicit_target_override"
else
	fail "explicit_target_override" "args=$(tr '\n' ' ' <"$TMP/explicit.bb.log")"
fi

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

duplicate_range_output="$TMP/duplicate_range_apk_download.out"
duplicate_range_status=0
if (cd "$FAKE_ROOT" && HOME="$TMP/duplicate_range_apk_download-home" \
	XDG_CACHE_HOME="$TMP/duplicate_range_apk_download-xdg-cache" \
	PATH="$STUB_BIN:$PATH" CI_FAKE_ROOT="$FAKE_ROOT" \
	BB_FIXTURE="$TMP/duplicate_range_apk_download" BB_STATUS=0 "$CI" test \
	>"$duplicate_range_output" 2>&1); then
	duplicate_range_status=0
else
	duplicate_range_status=$?
fi
duplicate_range_note="duplicate Range in apk download for https://packages.wolfi.dev/os/x86_64/erlang-27-27.3.4.2-r0.apk"
duplicate_range_note_line="$(grep -nF "$duplicate_range_note" "$duplicate_range_output" | cut -d: -f1 || true)"
duplicate_range_summary_line="$(grep -nF "no bazel test summary was found" "$duplicate_range_output" | cut -d: -f1 || true)"
if [[ "$duplicate_range_status" -eq 1 ]] &&
	grep -qF "$duplicate_range_note" "$duplicate_range_output" &&
	grep -qF "aborted before any test ran; re-running usually succeeds (issue #4849)" "$duplicate_range_output" &&
	[[ "$duplicate_range_note_line" -lt "$duplicate_range_summary_line" ]]; then
	pass "duplicate_range_apk_download_diagnosed"
else
	fail "duplicate_range_apk_download_diagnosed" "expected duplicate Range diagnosis before missing summary, got exit $duplicate_range_status: $(tr '\n' ' ' <"$duplicate_range_output")"
fi

# Execute the production formatter body against a local Git remote. A new
# package must be written back even when every tracked file is already clean.
format_body="$TMP/format-body.sh"
awk '
    $0 == "          commit_format_drift() {" { capture = 1 }
    capture && $0 == "          if [ \"$ON_MAIN\" = false ]; then" { exit }
    capture { sub(/^          /, ""); print }
' "$BUILD_BUDDY" >"$format_body"
format_call_ln=$(grep -nF '            prepare_go_build_graph' "$BUILD_BUDDY" | head -1 | cut -d: -f1 || true)
reachability_ln=$(grep -nF 'python3 ./bazel/tools/ci/test_source_reachability.py' "$BUILD_BUDDY" | head -1 | cut -d: -f1 || true)
if [[ -s "$format_body" && -n "$format_call_ln" && "$format_call_ln" -lt "$reachability_ln" ]]; then
	pass "format_before_graph_analysis"
else
	fail "format_before_graph_analysis" "CI generation must precede reachability and affected tests"
fi

if [[ -s "$format_body" ]]; then
	real_git=$(command -v git)
	for scenario in new-package tracked-change clean queue main generator-failure existing-untracked; do
		graph_root="$TMP/graph-$scenario"
		mkdir -p "$graph_root/work"
		"$real_git" init -q --bare "$graph_root/remote.git"
		"$real_git" -C "$graph_root/work" init -q -b feature
		"$real_git" -C "$graph_root/work" config user.name author
		"$real_git" -C "$graph_root/work" config user.email author@example.invalid
		printf 'initial\n' >"$graph_root/work/BUILD"
		"$real_git" -C "$graph_root/work" add BUILD
		"$real_git" -C "$graph_root/work" commit -qm initial
		"$real_git" -C "$graph_root/work" remote add origin "$graph_root/remote.git"
		"$real_git" -C "$graph_root/work" push -q origin feature
		before_graph=$($real_git -C "$graph_root/work" rev-parse HEAD)
		for guard in bazel/images/validate-generate-scripts.sh bazel/tools/format/check-migration-ordering.sh bazel/tools/git/check-commit-msg-ascii.sh bazel/tools/hooks/validate-hooks-executable.sh; do
			mkdir -p "$graph_root/work/${guard%/*}"
			printf '#!/usr/bin/env bash\nexit 0\n' >"$graph_root/work/$guard"
			chmod +x "$graph_root/work/$guard"
		done
		if [[ "$scenario" == existing-untracked ]]; then
			mkdir -p "$graph_root/work/unrelated"
			printf 'unrelated\n' >"$graph_root/work/unrelated/BUILD"
		fi
		graph_rc=0
		set +e
		(
			cd "$graph_root/work"
			set -eu
			ON_MAIN=false
			SKIP_MAIN_PUBLISH=false
			GIT_BRANCH=feature
			GHCR_TOKEN=unused-test-value
			[[ "$scenario" != main ]] || ON_MAIN=true
			[[ "$scenario" != queue ]] || GIT_BRANCH=gh-readonly-queue/main/pr-1
			git() {
				# No credentials or external network in this regression.
				if [[ "$1 $2" == 'remote set-url' ]]; then return 0; fi
				"$real_git" "$@"
			}
			python3() { :; }
			bazel() {
				if [[ "$*" == 'run //:gazelle --config=ci -- -lang=go,proto' ]]; then
					[[ "$scenario" != generator-failure ]] || return 7
					printf 'ran\n' >"$graph_root/formatted"
					if [[ "$scenario" != clean ]]; then
						mkdir -p generated
						printf 'generated\n' >generated/BUILD.bazel
						printf 'not generated source\n' >runner-private.tmp
					fi
					if [[ "$scenario" == tracked-change ]]; then
						printf 'updated\n' >BUILD
					fi
				fi
			}
			source "$format_body"
			prepare_go_build_graph
		) >"$graph_root/output" 2>&1
		graph_rc=$?
		set -e
		after_graph=$($real_git -C "$graph_root/work" rev-parse HEAD)
		remote_graph=$($real_git --git-dir="$graph_root/remote.git" rev-parse refs/heads/feature)
		case "$scenario" in
		new-package | tracked-change)
			if [[ "$graph_rc" -eq 1 && "$before_graph" != "$after_graph" && "$remote_graph" == "$after_graph" ]] &&
				"$real_git" -C "$graph_root/work" cat-file -e HEAD:generated/BUILD.bazel &&
				! "$real_git" -C "$graph_root/work" cat-file -e HEAD:runner-private.tmp 2>/dev/null; then
				pass "generated_graph_$scenario"
			else
				fail "generated_graph_$scenario" "write-back must include new BUILD files, exclude other untracked files and fail the superseded head: $(cat "$graph_root/output")"
			fi
			;;
		main | queue)
			if [[ "$graph_rc" -eq 1 && "$before_graph" == "$after_graph" && "$remote_graph" == "$before_graph" ]]; then
				pass "generated_graph_${scenario}_read_only"
			else
				fail "generated_graph_${scenario}_read_only" "drift must fail without committing or pushing"
			fi
			;;
		generator-failure)
			if [[ "$graph_rc" -eq 7 && "$before_graph" == "$after_graph" && "$remote_graph" == "$before_graph" ]]; then
				pass "generated_graph_failure_stops"
			else
				fail "generated_graph_failure_stops" "generator failure must stop before writeback and tests"
			fi
			;;
		existing-untracked)
			if [[ "$graph_rc" -eq 1 && "$before_graph" == "$after_graph" && "$remote_graph" == "$before_graph" && ! -f "$graph_root/formatted" ]]; then
				pass "generated_graph_preexisting_build_refused"
			else
				fail "generated_graph_preexisting_build_refused" "pre-existing runner BUILD files cannot become trusted generated source"
			fi
			;;
		clean)
			if [[ "$graph_rc" -eq 0 && "$before_graph" == "$after_graph" && "$remote_graph" == "$before_graph" ]]; then
				pass "generated_graph_$scenario"
			else
				fail "generated_graph_$scenario" "clean heads must reach the following test stage without another write-back"
			fi
			;;
		esac
	done
fi

# Run the entire production workflow, including commands after formatting.
# Only external commands and repository tools are stubbed; classification and
# stage boundaries come verbatim from buildbuddy.yaml. Git diffs use real trees.
workflow_body="$TMP/writeback-workflow.sh"
awk '
    /^      - run: \|$/ { capture = 1; next }
    capture && /^          / { sub(/^          /, ""); print; next }
    capture && /^$/ { print; next }
    capture { exit }
' "$BUILD_BUDDY" >"$workflow_body"
real_git=$(command -v git)
for scenario in qualifying chart-only app-only multi-project mixed ordinary handmade formatter pr-main queue feature missing-branch missing-default missing-parent diff-failure partial-diff empty rename-in rename-out nested newline space; do
	workflow_root="$TMP/workflow-$scenario"
	mkdir -p "$workflow_root"
	"$real_git" -C "$workflow_root" init -q -b fixture
	"$real_git" -C "$workflow_root" config user.name author
	"$real_git" -C "$workflow_root" config user.email author@example.invalid
	mkdir -p "$workflow_root/projects/service/chart" "$workflow_root/projects/service/deploy"
	printf 'version: 1.0.0\n' >"$workflow_root/projects/service/chart/Chart.yaml"
	printf 'targetRevision: 1.0.0\n' >"$workflow_root/projects/service/deploy/application.yaml"
	printf 'source\n' >"$workflow_root/source.py"
	"$real_git" -C "$workflow_root" add .
	"$real_git" -C "$workflow_root" commit -qm 'chore(charts): publish initial'
	"$real_git" -C "$workflow_root" update-ref refs/remotes/origin/main HEAD
	case "$scenario" in
	app-only) printf 'targetRevision: 1.0.1\n' >"$workflow_root/projects/service/deploy/application.yaml" ;;
	empty | missing-parent) ;;
	rename-in) "$real_git" -C "$workflow_root" mv -f source.py projects/service/chart/Chart.yaml ;;
	rename-out) "$real_git" -C "$workflow_root" mv projects/service/chart/Chart.yaml outside.yaml ;;
	*) printf 'version: 1.0.1\n' >"$workflow_root/projects/service/chart/Chart.yaml" ;;
	esac
	case "$scenario" in
	qualifying) printf 'targetRevision: 1.0.1\n' >"$workflow_root/projects/service/deploy/application.yaml" ;;
	multi-project)
		mkdir -p "$workflow_root/projects/other/deploy"
		printf 'targetRevision: 1.0.1\n' >"$workflow_root/projects/other/deploy/application.yaml"
		;;
	mixed | handmade) printf 'changed\n' >"$workflow_root/source.py" ;;
	nested | newline | space)
		project='nested/service'
		[[ "$scenario" != newline ]] || project=$'service\nother'
		[[ "$scenario" != space ]] || project='service other'
		mkdir -p "$workflow_root/projects/$project/chart"
		printf 'version: 1.0.1\n' >"$workflow_root/projects/$project/chart/Chart.yaml"
		;;
	esac
	subject='chore(charts): publish 1 chart version(s)'
	[[ "$scenario" != ordinary && "$scenario" != handmade ]] || subject='chore: update chart'
	[[ "$scenario" != formatter ]] || subject='style: auto-format'
	if [[ "$scenario" != missing-parent ]]; then
		"$real_git" -C "$workflow_root" add .
		"$real_git" -C "$workflow_root" commit -q --allow-empty -m "$subject"
	fi
	# Relative workflow tools record their invocation without running build or
	# publish code. No network, credentials or real snapshot cleanup is used.
	for tool in bazel/images/generate-push-all.sh bazel/helm/ci-diff-manifests.sh bazel/images/push/push-changed.sh bazel/helm/write-back-versions.sh bazel/images/validate-generate-scripts.sh bazel/tools/format/check-migration-ordering.sh bazel/tools/git/check-commit-msg-ascii.sh bazel/tools/hooks/validate-hooks-executable.sh projects/monolith/dev/grimoire/ci-runner.sh; do
		mkdir -p "$workflow_root/${tool%/*}"
		printf '#!/usr/bin/env bash\nprintf "tool %%s\\n" "$0" >> "$TRACE"\n' >"$workflow_root/$tool"
		chmod +x "$workflow_root/$tool"
	done
	mkdir -p "$workflow_root/bazel/tools/ci"
	printf '#!/usr/bin/env bash\necho //fixture:test\n' >"$workflow_root/bazel/tools/ci/affected-targets.sh"
	chmod +x "$workflow_root/bazel/tools/ci/affected-targets.sh"
	for failed_guard in none verification_genrule_guard central_build_ratchet chart_version_guard source_ratchet test_source_reachability; do
		# Failure injection applies to the qualifying path only.
		[[ "$scenario" == qualifying || "$failed_guard" == none ]] || continue
		export TRACE="$workflow_root/trace-$failed_guard"
		rc=0
		set +e
		(
			cd "$workflow_root"
			export GIT_BRANCH=main GIT_REPO_DEFAULT_BRANCH=main GIT_PR_NUMBER=0 GHCR_TOKEN=fixture
			case "$scenario" in
			pr-main) GIT_PR_NUMBER=7 ;;
			queue) GIT_BRANCH=gh-readonly-queue/main/pr-7 ;;
			feature) GIT_BRANCH=feature ;;
			missing-branch) unset GIT_BRANCH ;;
			missing-default) unset GIT_REPO_DEFAULT_BRANCH ;;
			esac
			git() {
				case "$1 $2" in
				'fetch origin' | 'remote set-url') return 0 ;;
				esac
				if [[ "$1" == diff && "$*" == *HEAD~1* ]]; then
					case "$scenario" in
					diff-failure) return 9 ;;
					partial-diff) printf 'projects/service/chart/Chart.yaml\0'; return 9 ;;
					esac
				fi
				if [[ "$scenario" == formatter && "$*" == "log -1 --format=%an" ]]; then echo ci-format-bot; return; fi
				"$real_git" "$@"
			}
			bazel() { printf 'bazel %s\n' "$*" >>"$TRACE"; }
			python3() {
				printf 'python3 %s\n' "$*" >>"$TRACE"
				[[ "$failed_guard" == none || "$1" != *"/$failed_guard.py" ]]
			}
			docker() { :; }
			df() { :; }
			du() { :; }
			rm() {
				if [[ "$*" == *sandbox_stash* ]]; then echo cleanup >>"$TRACE"; else command rm "$@"; fi
			}
			source "$workflow_body"
		) >"$workflow_root/output-$failed_guard" 2>&1
		rc=$?
		set -e
		if [[ "$failed_guard" != none ]]; then
			if [[ "$rc" -ne 0 ]] && ! grep -q '^bazel test\|^tool ' "$TRACE" && grep -q '^cleanup$' "$TRACE"; then
				pass "workflow_writeback_fatal_$failed_guard"
			else
				fail "workflow_writeback_fatal_$failed_guard" "guard failure must stop the fast path: $(cat "$TRACE")"
			fi
			continue
		fi
		if [[ "$rc" -ne 0 ]]; then
			fail "workflow_$scenario" "workflow failed: $(cat "$workflow_root/output-$failed_guard")"
			continue
		fi
		for guard in verification_genrule_guard central_build_ratchet chart_version_guard source_ratchet test_source_reachability; do
			grep -q "/$guard.py" "$TRACE" || fail "workflow_${scenario}_$guard" 'head guard was skipped'
		done
		grep -q '^cleanup$' "$TRACE" || fail "workflow_${scenario}_cleanup" 'snapshot hygiene was skipped'
		case "$scenario" in
		qualifying | chart-only | app-only | multi-project)
			if ! grep -q '^bazel test\|push_all\|push-changed.sh\|write-back-versions.sh\|check_go_stubs' "$TRACE"; then
				pass "workflow_${scenario}_no_tests_images_publish"
			else
				fail "workflow_$scenario" "fast path leaked expensive work: $(cat "$TRACE")"
			fi
			;;
		*)
			if grep -q '^bazel test .*gazelle_test' "$TRACE" && grep -q '^bazel test .*fixture:test\|^bazel test //...\|--target_pattern_file=' "$TRACE" && grep -q check_go_stubs "$TRACE"; then
				pass "workflow_${scenario}_ordinary_gates"
			else
				fail "workflow_$scenario" "fallback lost tests or generation acceptance: $(cat "$TRACE")"
			fi
			case "$scenario" in
			ordinary | handmade | formatter | missing-branch | missing-default | pr-main | queue | feature)
				grep -q 'push_all\|push-changed.sh' "$TRACE" || fail "workflow_${scenario}_images" 'ordinary image gate was skipped'
				;;
			esac
			;;
		esac
	done
done

echo "--- $PASS passed, $FAIL failed ---"
[[ $FAIL -eq 0 ]]
