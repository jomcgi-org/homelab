#!/usr/bin/env bash
# Hermetic tests for check-migration-ordering.sh. Drives the script against
# fixture directories with the base versions overridden (MIGRATION_BASE_VERSIONS),
# so it needs no git and runs in the Bazel sandbox.
set -uo pipefail

SCRIPT="${SCRIPT:-$(dirname "$0")/check-migration-ordering.sh}"

fail=0
_case() { # name expected_rc base_versions files...
	local name="$1" want="$2" bases="$3"
	shift 3
	local dir
	dir="$(mktemp -d)/migrations"
	mkdir -p "$dir"
	for f in "$@"; do : >"$dir/$f"; done
	MIGRATION_BASE_VERSIONS="$bases" bash "$SCRIPT" "$dir/x.sql" >/dev/null 2>&1
	local got=$?
	rm -rf "$dir"
	if [ "$got" -ne "$want" ]; then
		echo "FAIL: $name — expected exit $want, got $got" >&2
		fail=1
	else
		echo "ok: $name"
	fi
}

# Clean: a new migration strictly after the base max.
_case "clean-after-max" 0 "20260703210000" \
	20260703210000_orchestrator.sql 20260703230000_new.sql

# Duplicate version prefix within the directory (the #3139 collision).
_case "duplicate-version" 1 "20260703210000" \
	20260703130000_a.sql 20260703130000_b.sql

# New migration below the base max (the #3141 / #3142 out-of-order).
_case "out-of-order-below-max" 1 "20260703210000" \
	20260703210000_orchestrator.sql 20260703140000_new.sql

# A rebased branch carries the base files too, so a new migration colliding with
# an existing base version shows up as a duplicate in the tree (Rule 1 catches it).
_case "cross-base-duplicate-after-rebase" 1 "20260703210000" \
	20260703210000_orchestrator.sql 20260703210000_dupe.sql

# No base (brand-new dir): uniqueness still enforced, ordering skipped.
_case "no-base-unique-ok" 0 "" \
	20260703120000_a.sql 20260703130000_b.sql
_case "no-base-duplicate-fails" 1 "" \
	20260703120000_a.sql 20260703120000_b.sql

if [ "$fail" -eq 0 ]; then
	echo "ALL PASS"
fi
exit "$fail"
