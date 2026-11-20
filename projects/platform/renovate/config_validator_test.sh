#!/usr/bin/env bash
# invariant: ROOT-4
set -euo pipefail

config=$1
shift
"$@" --no-global "$config"

invalid="$TEST_TMPDIR/invalid.json"
echo '{"unknownRenovateOption": true}' >"$invalid"
if "$@" --no-global "$invalid" >"$TEST_TMPDIR/validator.log" 2>&1; then
	echo "Renovate accepted an unknown option" >&2
	exit 1
fi
cat "$TEST_TMPDIR/validator.log"
grep -Fq 'Invalid configuration option: unknownRenovateOption' "$TEST_TMPDIR/validator.log"
