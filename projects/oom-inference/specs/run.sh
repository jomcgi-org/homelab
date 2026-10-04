#!/usr/bin/env bash
# Model-check the oom-inference protocol specs with TLC.
#
#   specs/run.sh            every config below
#   specs/run.sh ci         the fast configs: small (baked: safety + liveness),
#                           small_indirect (safety), live_indirect (safety +
#                           liveness)
#   specs/run.sh <name>     one config (<name>.cfg)
#   specs/run.sh bugs       every buggy variant; each one must FAIL
#
# TLA_HOME holds tla2tools.jar and TLC's scratch dirs (default on the data
# disk, since the root disk on the 4090 box is nearly full).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
TLA_HOME=${TLA_HOME:-/disks/nvme-02/src/.toolchains/tla}
jar=$TLA_HOME/tla2tools.jar
work=$(mktemp -d "$TLA_HOME/run.XXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/tmp"
# TLC resolves the config next to the spec, so check a copy of the specs.
cp "$here"/*.tla "$here"/*.cfg "$here"/bug.cfg.in "$work/"

tlc() { # tlc <cfg path> <label>
	nice -n 19 taskset -c "${TLC_CPUS:-12-15}" java -Djava.io.tmpdir="$work/tmp" -XX:+UseParallelGC \
		-cp "$jar" tlc2.TLC -workers 4 -metadir "$work/$2" -config "$(basename "$1")" "$work/MC.tla"
}

run_cfg() { # run_cfg <name>: must pass
	echo "== $1"
	if tlc "$work/$1.cfg" "$1" >"$work/$1.log" 2>&1 && grep -q 'No error has been found' "$work/$1.log"; then
		grep -E 'distinct states found|^Finished in' "$work/$1.log"
	else
		tail -40 "$work/$1.log"
		echo "FAIL: $1"
		return 1
	fi
}

run_bugs() { # every variant must violate an invariant
	local rc=0
	for pair in skip_kernel_pin:baked skip_copy_pin:baked skip_graph_pin:baked \
		table_before_complete:baked skip_kernel_pin:indirect \
		write_before_consumed:indirect skip_table_pin:indirect; do
		local bug=${pair%%:*} mode=${pair##*:}
		sed -e "s/@BUG@/$bug/" -e "s/@MODE@/$mode/" "$work/bug.cfg.in" >"$work/$bug-$mode.cfg"
		tlc "$work/$bug-$mode.cfg" "$bug-$mode" >"$work/$bug-$mode.log" 2>&1 || true
		if grep -q 'is violated' "$work/$bug-$mode.log"; then
			echo "== $bug ($mode): caught: $(grep -m1 'is violated' "$work/$bug-$mode.log" | sed 's/^Error: //')"
		else
			tail -20 "$work/$bug-$mode.log"
			echo "FAIL: $bug ($mode) was not caught"
			rc=1
		fi
	done
	return $rc
}

case "${1:-all}" in
all)
	run_cfg small
	run_cfg small_indirect
	run_cfg live_indirect
	run_cfg large
	run_cfg large_indirect
	;;
ci)
	run_cfg small
	run_cfg small_indirect
	run_cfg live_indirect
	;;
bugs) run_bugs ;;
*) run_cfg "$1" ;;
esac
