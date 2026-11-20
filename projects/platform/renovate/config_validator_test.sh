#!/usr/bin/env bash
# invariant: ROOT-4
set -euo pipefail

# Renovate installs its validator in /usr/local/sbin in the test image.
export PATH="/usr/local/sbin:$PATH"
renovate-config-validator --no-global "$1"

invalid="$TEST_TMPDIR/invalid.json"
echo '{"unknownRenovateOption": true}' >"$invalid"
if renovate-config-validator --no-global "$invalid" >"$TEST_TMPDIR/validator.log" 2>&1; then
	echo "Renovate accepted an unknown option" >&2
	exit 1
fi
grep -F 'Invalid configuration option: unknownRenovateOption' "$TEST_TMPDIR/validator.log"
