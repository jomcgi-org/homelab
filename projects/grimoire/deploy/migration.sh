#!/bin/sh
# Reads the newest complete Grimoire migration run from the private R2 bucket.
# The hub job grimoire-migration-dump writes each run under
# R2_BUCKET/MIGRATION_PREFIX<run id>/ and writes its SHA256SUMS last, so a run
# with SHA256SUMS is complete (projects/monolith/grimoire/migration.py).
#
#   migration.sh fetch DIR  copies the restore inputs to DIR and checks them
#   migration.sh check      checks every object of the run against SHA256SUMS
#
# rclone reads the remote R2 from the RCLONE_CONFIG_R2_* variables.
set -eu
: "${R2_BUCKET:?}" "${MIGRATION_PREFIX:?}"
run() { rclone --config /dev/null "$@"; }
root="R2:$R2_BUCKET/$MIGRATION_PREFIX"

# Run ids are UTC timestamps, so the last one in sort order is the newest.
# A failed listing stops the script here.
listing=$(run lsf --recursive --max-depth 2 --files-only --include '/*/SHA256SUMS' "$root")
latest=$(printf '%s\n' "$listing" | grep -E '^[0-9]{8}T[0-9]{6}Z/SHA256SUMS$' | sort | tail -n 1)
if [ -z "$latest" ]; then
	echo "no complete run under $root" >&2
	exit 1
fi
id=${latest%/SHA256SUMS}
echo "run $MIGRATION_PREFIX$id"

case "${1:-}" in
fetch)
	dir=${2:?usage: migration.sh fetch DIR}
	for name in SHA256SUMS grimoire.dump vector.metadata source.inventory; do
		run copyto "$root$id/$name" "$dir/$name"
	done
	cd "$dir"
	grep -E '^[0-9a-f]{64}  (grimoire\.dump|vector\.metadata|source\.inventory)$' SHA256SUMS >inputs.sha256
	if [ "$(wc -l <inputs.sha256)" -ne 3 ]; then
		echo "SHA256SUMS does not list the three restore inputs" >&2
		exit 1
	fi
	sha256sum -c inputs.sha256
	;;
check)
	tmp=$(mktemp -d)
	trap 'rm -rf "$tmp"' EXIT
	run copyto "$root$id/SHA256SUMS" "$tmp/SHA256SUMS"
	# --download reads each object, because R2 stores no SHA-256 for it.
	run checksum sha256 --download "$tmp/SHA256SUMS" "$root$id" --exclude /SHA256SUMS
	echo "run $MIGRATION_PREFIX$id: $(wc -l <"$tmp/SHA256SUMS") objects match SHA256SUMS"
	run copyto "$root$id/missing-references.txt" "$tmp/missing"
	echo "run $MIGRATION_PREFIX$id: $(wc -l <"$tmp/missing") referenced objects missing"
	;;
*)
	echo "usage: migration.sh fetch DIR | migration.sh check" >&2
	exit 2
	;;
esac
