#!/usr/bin/env bash
# Run TLC (the TLA+ model checker) over one spec + cfg on the RBE executor, in
# one of two expectation modes, for the ADR embervm/006 pilot.
#
# Invoked from the //projects/embervm/specs genrules. TLC writes scratch state
# (a states/ dir, .st checkpoint droppings) NEXT TO the spec it checks, so the
# driver stages the spec + cfg into a writable mktemp workdir before running,
# the same "everything writable" discipline as bazel/erlang/mix_test.sh.
#
# Before checking, the driver also proves the committed PlusCal translation is
# fresh: it re-runs pcal.trans on a copy of the spec and diffs, so an edit to the
# PlusCal algorithm block that was not re-translated fails loudly instead of
# checking a stale TLA+ body.
#
# Args:
#   $1 java anchor (the JRE's bin/java)
#   $2 tla2tools.jar
#   $3 spec .tla
#   $4 cfg
#   $5 output marker (TLC output is captured here; also the genrule's out)
#   $6 expectation: "pass" (TLC must find no error) or "fail" (a negative mode
#      that must still reproduce a known violation)
set -euo pipefail

java_anchor="$1"
jar="$2"
spec="$3"
cfg="$4"
out="$5"
expect="$6"

# Absolutize every input before we cd into the workdir (Bazel passes them
# execroot-relative).
abspath() {
	case "$1" in
	/*) printf '%s' "$1" ;;
	*) printf '%s/%s' "$(pwd)" "$1" ;;
	esac
}
java_anchor="$(abspath "$java_anchor")"
jar="$(abspath "$jar")"
spec="$(abspath "$spec")"
cfg="$(abspath "$cfg")"
out="$(abspath "$out")"

java="$java_anchor"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Stage the spec + cfg writable; TLC drops state/checkpoint dirs alongside them.
spec_name="$(basename "$spec")" # e.g. adoption.tla
module="${spec_name%.tla}"      # e.g. adoption
cp "$spec" "$work/$spec_name"
cp "$cfg" "$work/$(basename "$cfg")"
cfg_name="$(basename "$cfg")"

cd "$work"

# --- Translation freshness check -------------------------------------------
# pcal.trans rewrites the .tla in place between the BEGIN/END TRANSLATION
# markers. Re-run it on a throwaway copy and diff against the committed spec: a
# difference means someone edited the PlusCal algorithm without re-translating.
cp "$spec_name" "trans_check.tla"
if ! "$java" -cp "$jar" pcal.trans -nocfg "trans_check.tla" >trans.log 2>&1; then
	echo "pcal.trans failed on $spec_name:" >&2
	cat trans.log >&2
	exit 1
fi
if ! diff -u "$spec_name" "trans_check.tla" >trans.diff; then
	echo "STALE PLUSCAL TRANSLATION in $spec_name:" >&2
	echo "The committed TLA+ translation does not match what pcal.trans produces" >&2
	echo "from the PlusCal block. Run 'java -cp tla2tools.jar pcal.trans $spec_name'" >&2
	echo "and commit the result. Diff (committed vs freshly translated):" >&2
	cat trans.diff >&2
	exit 1
fi
rm -f "trans_check.tla"

# --- Model check ------------------------------------------------------------
# -Dtlc2.TLC.stopAfter=600 bounds wall time (seconds) so a runaway check cannot
# hang the CI action; -workers auto uses the executor's cores. Note TLC exits 0
# when the bound fires, so pass mode below also checks the queue drained.
set +e
"$java" -XX:+UseParallelGC -Dtlc2.TLC.stopAfter=600 \
	-cp "$jar" tlc2.TLC -workers auto -config "$cfg_name" "$module" >"$out" 2>&1
tlc_rc=$?
set -e

if [ "$expect" = "pass" ]; then
	if [ "$tlc_rc" -ne 0 ]; then
		echo "TLC found an error checking $spec_name (expected: no error). Trace:" >&2
		cat "$out" >&2
		exit 1
	fi
	# A zero exit is NOT proof of an exhaustive check: when stopAfter fires, TLC
	# prints "No error has been found", leaves states on the queue, and still
	# exits 0 (#4050). Require the completion line to report an empty queue so a
	# truncated search reads as INCOMPLETE, never as PASS.
	if ! grep -qE 'states generated, .*, 0 states left on queue' "$out"; then
		echo "TLC INCOMPLETE: $spec_name was NOT checked exhaustively. TLC exited 0" >&2
		echo "but its output does not report '0 states left on queue', so the state" >&2
		echo "space was truncated (most likely the -Dtlc2.TLC.stopAfter wall-time" >&2
		echo "bound fired) and a clean result proves nothing. Output:" >&2
		cat "$out" >&2
		exit 1
	fi
	echo "TLC PASS: $spec_name checked clean (exhaustive)." >&2
elif [ "$expect" = "fail" ]; then
	# Negative mode: the model must still reproduce the historical violation. A
	# zero exit means TLC found no error, i.e. the model went blind to the bug it
	# is meant to guard, so we fail. We also require the output to name an actual
	# invariant/temporal violation, so a TLC crash (also nonzero) is not mistaken
	# for a detection.
	if [ "$tlc_rc" -eq 0 ]; then
		echo "NEGATIVE MODE REGRESSED: TLC found NO error for $spec_name, but this" >&2
		echo "config exists to prove the model still detects a historical bug. The" >&2
		echo "model has gone blind. Output:" >&2
		cat "$out" >&2
		exit 1
	fi
	if ! grep -qE 'Invariant|Temporal properties were violated' "$out"; then
		echo "NEGATIVE MODE did not reproduce a violation for $spec_name: TLC exited" >&2
		echo "nonzero but its output names no invariant or temporal-property" >&2
		echo "violation (likely a crash, not a detection). Output:" >&2
		cat "$out" >&2
		exit 1
	fi
	echo "TLC negative mode OK: $spec_name reproduced the expected violation." >&2
else
	echo "unknown expectation '$expect' (want 'pass' or 'fail')" >&2
	exit 1
fi
