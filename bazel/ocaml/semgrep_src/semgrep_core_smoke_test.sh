#!/bin/sh
set -eu

engine=$1
rules=$2
target=$3
targets_json="$TEST_TMPDIR/targets.json"

printf '["Targets",[["CodeTarget",{"path":{"fpath":"%s","ppath":"semgrep_core_smoke_target.go"},"analyzer":"go","products":["sast"]}]]]\n' \
	"$target" >"$targets_json"

output=$("$engine" "$rules" "$targets_json")
if [ "$output" != "matches=1 errors=0" ]; then
	echo "semgrep-core did not produce the expected native Go match" >&2
	echo "$output" >&2
	exit 1
fi
