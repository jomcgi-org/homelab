#!/usr/bin/env bash
# Enforce linear, non-duplicated Atlas migration history.
#
# Atlas versions a migration on the leading timestamp of its filename and
# refuses to apply a directory whose history is not strictly linear. Two files
# sharing a version ("multiple files with the same version"), or a migration
# whose version is below one already applied ("added out of order"), wedge the
# ENTIRE migration pipeline at apply time, not just the offending file, and
# `atlas migrate validate` does not catch either before merge. This guard does.
#
# Per migrations/ directory it enforces:
#   1. every version prefix is unique, and
#   2. any version not already on the base ref is strictly greater than the
#      base ref's current maximum version (never inserted below an existing,
#      possibly-already-applied migration).
#
# Runs in the CI "Format check" (where origin/main is fetched) and as a local
# pre-commit hook. Base versions come from `git ls-tree $MIGRATION_BASE_REF`
# (default origin/main); tests override them hermetically via
# MIGRATION_BASE_VERSIONS="<version> <version> ...".
set -euo pipefail

BASE_REF="${MIGRATION_BASE_REF:-origin/main}"

# Extract the leading 14-digit timestamp version of each migration filename.
# Files that do not match the convention are ignored (not this check's concern).
_versions() { sed -n 's#.*/\([0-9]\{14\}\)_[^/]*\.sql$#\1#p'; }

# Directories to check: the parents of any migration args (pre-commit passes the
# changed files), or every migrations/ dir in the tree when run with no args.
declare -A DIRS=()
if [ "$#" -gt 0 ]; then
	for f in "$@"; do
		case "$f" in */migrations/*.sql) DIRS["${f%/*}"]=1 ;; esac
	done
else
	while IFS= read -r f; do DIRS["${f%/*}"]=1; done < <(git ls-files '*migrations/*.sql')
fi
if [ "${#DIRS[@]}" -eq 0 ]; then
	echo "no migration directories to check"
	exit 0
fi

rc=0
for dir in "${!DIRS[@]}"; do
	wt="$(find "$dir" -maxdepth 1 -name '*.sql' | _versions | sort || true)"
	[ -z "$wt" ] && continue

	# Rule 1: unique versions within the directory.
	dupes="$(printf '%s\n' "$wt" | uniq -d)"
	if [ -n "$dupes" ]; then
		rc=1
		while IFS= read -r v; do
			[ -z "$v" ] && continue
			echo "ERROR [$dir]: duplicate migration version $v:" >&2
			find "$dir" -maxdepth 1 -name "${v}_*.sql" | sort | sed 's/^/    /' >&2
		done <<<"$dupes"
	fi

	# Resolve the base versions this directory must build on top of.
	if [ -n "${MIGRATION_BASE_VERSIONS+x}" ]; then
		# shellcheck disable=SC2086 # intentional word-split of the space-separated override
		base="$(printf '%s\n' $MIGRATION_BASE_VERSIONS | sort)"
	elif git rev-parse --verify --quiet "$BASE_REF" >/dev/null 2>&1; then
		base="$(git ls-tree -r --name-only "$BASE_REF" -- "$dir" | _versions | sort || true)"
	else
		echo "WARN [$dir]: $BASE_REF not found; enforcing uniqueness only (ordering needs the base ref)" >&2
		base=""
	fi

	# Rule 2: every version new vs the base must be strictly after the base max.
	if [ -n "$base" ]; then
		max_base="$(printf '%s\n' "$base" | tail -n1)"
		while IFS= read -r v; do
			[ -z "$v" ] && continue
			printf '%s\n' "$base" | grep -qxF "$v" && continue # already on base
			if [ ! "$v" \> "$max_base" ]; then
				rc=1
				echo "ERROR [$dir]: new migration $v is not after ${BASE_REF}'s latest ($max_base)." >&2
				echo "    Rename it to a timestamp greater than $max_base; Atlas needs linear," >&2
				echo "    non-duplicated history or the whole pipeline wedges at apply time." >&2
			fi
		done <<<"$wt"
	fi
done

if [ "$rc" -eq 0 ]; then
	echo "PASS: migration versions are unique and ordered after $BASE_REF."
fi
exit "$rc"
