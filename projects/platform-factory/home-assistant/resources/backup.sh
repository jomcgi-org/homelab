#!/bin/sh
# backup.sh backup: copy one volume, mounted read-only at /data, into Garage
# as a tar.gz, and delete the archives older than KEEP_DAYS.
# backup.sh restore: extract the newest archive into /data.
# backup.yaml sets:
#   NAME      object prefix and archive name
#   EXCLUDES  tar exclude globs, separated by spaces
#   REQUIRED  paths that the archive must hold, separated by spaces
#   KEEP_DAYS retention in days
# The delete runs only after an upload succeeds, so the newest archive stays.
set -eu

check() {
	listing=$(tar -tzf "$1")
	for path in $REQUIRED; do
		if ! printf '%s\n' "$listing" | grep -qxF "./$path"; then
			echo "$1 has no $path" >&2
			exit 1
		fi
	done
}

if [ "${1:-}" = restore ]; then
	# A failed listing stops the run here. Archive names sort by UTC time.
	names=$(rclone lsf --files-only "garage:$BUCKET/$NAME/")
	newest=$(printf '%s\n' "$names" | sort | tail -n 1)
	if [ -z "$newest" ]; then
		echo "no archive in garage:$BUCKET/$NAME/" >&2
		exit 1
	fi
	rclone copyto "garage:$BUCKET/$NAME/$newest" /tmp/restore.tar.gz
	check /tmp/restore.tar.gz
	tar -xzf /tmp/restore.tar.gz -C /data
	echo "restored $newest into /data"
	exit 0
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="/tmp/$NAME-$stamp.tar.gz"

set -f
excludes=""
for pattern in $EXCLUDES; do
	excludes="$excludes --exclude $pattern"
done
# shellcheck disable=SC2086 # one word for each exclude option
tar -czf "$archive" -C /data $excludes .
set +f

check "$archive"

dest="garage:$BUCKET/$NAME/$NAME-$stamp.tar.gz"
rclone copyto "$archive" "$dest"
echo "uploaded:"
rclone lsl "$dest"
rclone delete --min-age "${KEEP_DAYS}d" "garage:$BUCKET/$NAME/"
echo "archives kept:"
rclone lsl "garage:$BUCKET/$NAME/"
