#!/usr/bin/env bash
# Unit test for the tlc.sh driver's verdicts (#4050). Runs the driver against a
# fake `java` that answers pcal.trans with a no-op (so the translation freshness
# check passes) and answers tlc2.TLC with canned output chosen per case, so no
# JRE or TLC jar is needed and the test is sub-second.
#
# The case that motivated this test: TLC exits 0 when -Dtlc2.TLC.stopAfter fires,
# printing "No error has been found" with states still on the queue. The driver
# must report that as INCOMPLETE, not PASS.
set -euo pipefail

driver="$(pwd)/$1"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Fake java. TLC_CASE selects the canned model-check output and exit code.
fake_java="$work/java"
cat >"$fake_java" <<'JAVA'
#!/usr/bin/env bash
# args: -cp jar pcal.trans ... | -XX:... -Dtlc2.TLC.stopAfter=N -cp jar tlc2.TLC ...
for a in "$@"; do
	case "$a" in
	pcal.trans) exit 0 ;;
	tlc2.TLC)
		case "$TLC_CASE" in
		exhaustive)
			echo "Model checking completed. No error has been found."
			echo "156554286 states generated, 30063250 distinct states found, 0 states left on queue."
			echo "The depth of the complete state graph search is 45."
			exit 0
			;;
		truncated)
			echo "Model checking completed. No error has been found."
			echo "12769001 states generated, 3426982 distinct states found, 852718 states left on queue."
			exit 0
			;;
		violation)
			echo "Error: Invariant NoDoubleDispatch is violated."
			echo "1234 states generated, 567 distinct states found, 89 states left on queue."
			exit 12
			;;
		esac
		;;
	esac
done
echo "fake java: unhandled args: $*" >&2
exit 99
JAVA
chmod +x "$fake_java"

: >"$work/tla2tools.jar"
echo "---- MODULE fake ----" >"$work/fake.tla"
echo "INIT Init" >"$work/fake.cfg"

run() { # $1 case, $2 expectation; prints driver rc, captures stderr in $work/err
	local rc=0
	TLC_CASE="$1" "$driver" "$fake_java" "$work/tla2tools.jar" "$work/fake.tla" \
		"$work/fake.cfg" "$work/out" "$2" >"$work/err" 2>&1 || rc=$?
	echo "$rc"
}

fail() {
	echo "FAIL: $1" >&2
	echo "--- driver stderr ---" >&2
	cat "$work/err" >&2
	exit 1
}

# pass mode, exhaustive: PASS
[ "$(run exhaustive pass)" -eq 0 ] || fail "exhaustive clean run should pass"
grep -q "TLC PASS" "$work/err" || fail "exhaustive run should print TLC PASS"

# pass mode, truncated: the #4050 case. Must be non-zero and say INCOMPLETE,
# and must NOT claim PASS.
[ "$(run truncated pass)" -ne 0 ] || fail "truncated run (states left on queue, exit 0) must not pass"
grep -q "TLC INCOMPLETE" "$work/err" || fail "truncated run should be reported as INCOMPLETE"
grep -q "TLC PASS" "$work/err" && fail "truncated run must not be reported as PASS"

# pass mode, violation: still fails
[ "$(run violation pass)" -ne 0 ] || fail "violation should fail pass mode"

# fail mode, violation: OK; fail mode, exhaustive clean: regressed
[ "$(run violation fail)" -eq 0 ] || fail "violation should satisfy fail mode"
[ "$(run exhaustive fail)" -ne 0 ] || fail "clean run must not satisfy fail mode"
grep -q "NEGATIVE MODE REGRESSED" "$work/err" || fail "clean fail-mode run should report regression"

echo "tlc_test: all cases OK"
