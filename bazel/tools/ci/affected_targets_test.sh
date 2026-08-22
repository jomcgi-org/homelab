#!/usr/bin/env bash
# Tests for affected-targets.sh
set -euo pipefail

SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/bazel/tools/ci/affected-targets.sh" \
	"${TEST_SRCDIR:-}/_main/bazel/tools/ci/affected-targets.sh" \
	"${BASH_SOURCE[0]%/*}/affected-targets.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$(cd "${candidate%/*}" && pwd)/${candidate##*/}"
		break
	fi
done
if [[ -z "$SCRIPT" ]]; then
	echo "ERROR: cannot locate affected-targets.sh" >&2
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

# Set up temporary test environment
TMP="${TEST_TMPDIR:-$(mktemp -d)}"
FAKE_BAZEL="$TMP/bazel"
WORK="$TMP/work"
mkdir -p "$WORK"

# Create a fake bazel command
mkdir -p "$(dirname "$FAKE_BAZEL")"
cat >"$FAKE_BAZEL" <<'EOFBAZEL'
#!/usr/bin/env bash
log_file="${BAZEL_LOG_FILE:-}"
if [[ -n "$log_file" ]]; then
	printf '%s\n' "$@" >> "$log_file"
fi
# Real bazel chatters on stderr; none of it may be parsed as a label.
echo "WARNING: fake bazel noise" >&2
echo "Loading: 0 packages loaded" >&2

# Look for "query" to identify this as a bazel query
for arg in "$@"; do
	if [[ "$arg" =~ ^set\( ]]; then
		# Label existence probe: extract labels from the set() call and output them
		# Format: set(//pkg:label1 //pkg:label2 ...)
		labels="${arg#set(}"
		labels="${labels%)}"
		[[ -n "$labels" ]] && printf '%s\n' $labels
		exit "${BAZEL_SET_EXIT:-0}"
	elif [[ "$arg" =~ ^rdeps\( ]]; then
		# rdeps query: output fixture if provided
		if [[ -n "${BAZEL_RDEPS_OUTPUT:-}" && -f "${BAZEL_RDEPS_OUTPUT}" ]]; then
			cat "${BAZEL_RDEPS_OUTPUT}"
		fi
		exit "${BAZEL_RDEPS_EXIT:-0}"
	fi
done
exit 0
EOFBAZEL
chmod +x "$FAKE_BAZEL"

run_test() {
	local name="$1" setup="$2"
	local repo="$WORK/$name"
	rm -rf "$repo"
	mkdir -p "$repo"
	cd "$repo"

	git init -q .
	git config user.email "t@x"
	git config user.name "T"

	$setup

	local out="$TMP/$name.out" err="$TMP/$name.err" log="$TMP/$name.log"
	rm -f "$log"
	if BAZEL="$FAKE_BAZEL" BAZEL_LOG_FILE="$log" "$SCRIPT" "origin/main" "HEAD" >"$out" 2>"$err"; then
		check_$name "$out" "$err" "$log"
	else
		check_$name "$out" "$err" "$log"
	fi
}

# Test 1: BUILD file is fallback
setup_build_chg() {
	mkdir -p p
	echo "" >p/BUILD
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "x" >p/BUILD
}

check_build_chg() {
	[[ "$(cat "$1")" == "//..." ]] && pass "BUILD_fallback" || fail "BUILD_fallback" "$(cat "$1")"
}

# Test 2: Root package label //:file
setup_root_label() {
	echo "" >BUILD
	echo "x" >tool.py
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "y" >tool.py
}

check_root_label() {
	grep -q "^set(.*//:tool.py" "$3" && pass "root_label" || fail "root_label" "$(cat "$3")"
}

# Test 3: Deleted file is fallback
setup_deleted() {
	mkdir -p p
	echo "" >p/BUILD
	echo "x" >p/old.py
	git add .
	git commit -q -m "base"
	git branch origin/main
	rm p/old.py
	git add p/old.py
}

check_deleted() {
	[[ "$(cat "$1")" == "//..." ]] && pass "deleted_fallback" || fail "deleted_fallback" "$(cat "$1")"
}

# Test 4: rdeps failure fallback
setup_rdeps_fail() {
	mkdir -p p
	echo "" >p/BUILD
	echo "x" >p/f.py
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "y" >p/f.py
}

check_rdeps_fail() {
	[[ "$(cat "$1")" == "//..." ]] && pass "rdeps_fail_fallback" || fail "rdeps_fail_fallback" "got $(cat "$1")"
}

# Fallback set: a file under bazel/, a lockfile, a .bzl
setup_bazel_dir() {
	mkdir -p bazel/tools p
	echo "" >p/BUILD
	echo "x" >bazel/tools/x.sh
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "y" >bazel/tools/x.sh
}
check_bazel_dir() {
	[[ "$(cat "$1")" == "//..." ]] && pass "bazel_dir_fallback" || fail "bazel_dir_fallback" "$(cat "$1")"
}
setup_lockfile() {
	mkdir -p p
	echo "" >p/BUILD
	echo "x" >p/requirements.lock
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "y" >p/requirements.lock
}
check_lockfile() {
	[[ "$(cat "$1")" == "//..." ]] && pass "lockfile_fallback" || fail "lockfile_fallback" "$(cat "$1")"
}
setup_bzl() {
	mkdir -p p
	echo "" >p/BUILD
	echo "x" >p/defs.bzl
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "y" >p/defs.bzl
}
check_bzl() {
	[[ "$(cat "$1")" == "//..." ]] && pass "bzl_fallback" || fail "bzl_fallback" "$(cat "$1")"
}

# rdeps --keep_going exit 3 with EMPTY output must fall back, never fail open
setup_rdeps_empty3() { setup_rdeps_fail; }
check_rdeps_empty3() {
	[[ "$(cat "$1")" == "//..." ]] && pass "rdeps_exit3_empty_fallback" || fail "rdeps_exit3_empty_fallback" "got '$(cat "$1")'"
}

# rules_py venv helper targets (<py_test>.venv) come back from rdeps beside the
# py_test; requesting both at top level is a Bazel action conflict (#5121), so
# the script must drop them and keep everything else.
setup_venv() { setup_rdeps_fail; }
check_venv() {
	local got
	got="$(cat "$1" | tr '\n' ' ' | sed 's/ *$//')"
	if [[ "$got" == "//p:chart_test //p:other_lib" ]]; then
		pass "venv_targets_dropped"
	else
		fail "venv_targets_dropped" "got '$got'"
	fi
}

# stderr noise from the fake bazel never leaks into stdout or the rdeps query
setup_noise() { setup_rdeps_fail; }
check_noise() {
	if grep -q "WARNING" "$1" || grep -q "Loading" "$3"; then
		fail "stderr_not_parsed" "noise leaked: stdout=$(cat "$1")"
	else
		pass "stderr_not_parsed"
	fi
}

# Test 5: Single set() probe call
setup_single_probe() {
	mkdir -p p
	echo "" >p/BUILD
	touch p/a.py p/b.py
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "x" >p/a.py
	echo "y" >p/b.py
}

check_single_probe() {
	local cnt=$(grep -c "^set(" "$3" || true)
	[[ $cnt -eq 1 ]] && pass "single_set_probe" || fail "single_set_probe" "got $cnt"
}

# Test 6: Args passed through (base head -- args)
setup_args() {
	mkdir -p p
	echo "" >p/BUILD
	echo "x" >p/f.py
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "y" >p/f.py
}

check_args() {
	grep -q "\-\-config=ci" "$3" && pass "args_passthrough" || fail "args_passthrough" "args missing"
}

# Test 7: .py file to label mapping
setup_py_map() {
	mkdir -p p
	cat >p/BUILD <<'EOF'
genrule(name = "g", srcs = ["f.py"], outs = ["o.txt"], cmd = "echo")
EOF
	echo "x" >p/f.py
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "y" >p/f.py
}

check_py_map() {
	grep "^set(" "$3" | grep -q "p:f.py" && pass "py_file_label" || fail "py_file_label" "label missing"
}

# Test 8: Nested packages a/BUILD a/b/BUILD
setup_nested() {
	mkdir -p a/b
	echo "" >a/BUILD
	echo "" >a/b/BUILD
	echo "x" >a/b/nested.go
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "y" >a/b/nested.go
}

check_nested() {
	grep "^set(" "$3" | grep -q "a/b:nested.go" && pass "nested_label" || fail "nested_label" "label missing"
}

# Test 9: File outside package
setup_outside() {
	mkdir -p p
	echo "" >p/BUILD
	echo "x" >README.md
	git add .
	git commit -q -m "base"
	git branch origin/main
	echo "y" >README.md
}

check_outside() {
	[[ -z "$(cat "$1")" ]] && pass "file_outside_pkg" || fail "file_outside_pkg" "expected empty"
}

# Test 10: No changes
setup_no_change() {
	mkdir -p p
	echo "" >p/BUILD
	git add .
	git commit -q -m "base"
	git branch origin/main
}

check_no_change() {
	[[ -z "$(cat "$1")" ]] && pass "no_changes" || fail "no_changes" "expected empty"
}

echo "--- Running affected-targets.sh tests ---"
run_test "build_chg" "setup_build_chg"
run_test "root_label" "setup_root_label"
run_test "deleted" "setup_deleted"
BAZEL_RDEPS_EXIT="1" run_test "rdeps_fail" "setup_rdeps_fail"
run_test "single_probe" "setup_single_probe"

# Special test for args: run with -- flag
repo="$WORK/args"
rm -rf "$repo"
mkdir -p "$repo"
cd "$repo"
git init -q .
git config user.email "t@x"
git config user.name "T"
setup_args
log="$TMP/args.log"
rm -f "$log"
BAZEL="$FAKE_BAZEL" BAZEL_LOG_FILE="$log" "$SCRIPT" "origin/main" "HEAD" -- --config=ci --deleted_packages=bazel/tools/python >/dev/null 2>&1 || true
grep -q "\-\-config=ci" "$log" && pass "args_passthrough" || fail "args_passthrough" "args missing"

# base -- args form (no explicit head) must not take "--" as the head ref
repo="$WORK/args2"
rm -rf "$repo"
mkdir -p "$repo"
cd "$repo"
git init -q .
git config user.email "t@x"
git config user.name "T"
setup_args
log="$TMP/args2.log"
rm -f "$log"
out="$TMP/args2.out"
BAZEL="$FAKE_BAZEL" BAZEL_LOG_FILE="$log" "$SCRIPT" "origin/main" -- --config=ci >"$out" 2>/dev/null || true
if grep -q "^--config=ci$" "$log" && ! grep -q "^--$" "$log" && [[ "$(cat "$out")" != "//..." ]]; then
	pass "args_without_head"
else
	fail "args_without_head" "log=$(tr '\n' ' ' <"$log") out=$(cat "$out")"
fi

run_test "bazel_dir" "setup_bazel_dir"
run_test "lockfile" "setup_lockfile"
run_test "bzl" "setup_bzl"
BAZEL_RDEPS_EXIT="3" run_test "rdeps_empty3" "setup_rdeps_empty3"
run_test "noise" "setup_noise"
venv_fixture="$TMP/rdeps_venv.txt"
printf '%s\n' "//p:chart_test" "//p:chart_test.venv" "//p:other_lib" >"$venv_fixture"
BAZEL_RDEPS_OUTPUT="$venv_fixture" run_test "venv" "setup_venv"
run_test "py_map" "setup_py_map"
run_test "nested" "setup_nested"
run_test "outside" "setup_outside"
run_test "no_change" "setup_no_change"

echo "--- $PASS passed, $FAIL failed ---"
[[ $FAIL -eq 0 ]]
